/**
 * Run Testcase Tab
 * ================
 * All execution parameters as dropdowns:
 *   - Environment  : dev / sit / uat
 *   - Browser      : chromium / firefox / webkit
 *   - Device       : Desktop Chrome / iPhone 13 / Pixel 7 / …
 *   - Mode         : headless / headed
 *   - Tags         : regression / smoke / sanity / e2e / critical
 *
 * Live log output streams via WebSocket.
 * Allure report link appears when run completes.
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import {
  Card, Select, Button, Tag, Space, Divider,
  Badge, Typography, Table, Tooltip,
} from 'antd';
import {
  PlayCircleOutlined, StopOutlined, LinkOutlined,
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
} from '@ant-design/icons';
import toast from 'react-hot-toast';
import { useQuery } from '@tanstack/react-query';
import { fetchScripts, fetchRuns, startRun, connectRunSocket, fetchRunLogs } from '../api/client';
import type { GeneratedScript, ExecutionRun, RunParams } from '../types';

const { Text } = Typography;
const { Option } = Select;

// ── Option lists ───────────────────────────────────────────────────────────────
const ENVIRONMENTS = ['dev', 'sit', 'uat'];
const BROWSERS     = ['chromium', 'firefox', 'webkit'];
const DEVICES      = [
  // Desktop
  'Desktop Chrome', 'Desktop Firefox', 'Desktop Safari',
  // Mobile — iPhone 13 confirmed
  'iPhone 13', 'iPhone 14', 'iPhone 15 Pro',
  'Pixel 7', 'Galaxy S23', 'Galaxy S24',
  // Tablet
  'iPad Pro', 'iPad Air',
];
const MODES          = ['headless', 'headed'];
const AVAILABLE_TAGS = ['regression', 'smoke', 'sanity', 'e2e', 'critical'];

const STATUS_ICON: Record<string, React.ReactNode> = {
  queued:  <LoadingOutlined style={{ color: '#faad14' }} />,
  running: <LoadingOutlined spin style={{ color: '#1677ff' }} />,
  passed:  <CheckCircleFilled style={{ color: '#52c41a' }} />,
  failed:  <CloseCircleFilled style={{ color: '#ff4d4f' }} />,
  error:   <CloseCircleFilled style={{ color: '#ff7875' }} />,
};

export default function RunTab() {
  const [selectedScriptId, setSelectedScriptId] = useState('');
  const [params, setParams] = useState<RunParams>({
    environment:    'sit',
    browser:        'chromium',
    device:         'Desktop Chrome',
    execution_mode: 'headless',
    browser_version:'stable',
    tags:           ['regression'],
  });
  const [running, setRunning]       = useState(false);
  const [currentRunId, setCurrentRunId] = useState('');
  const [logs, setLogs]             = useState<string[]>([]);
  const [runStatus, setRunStatus]   = useState<string>('');
  const [ghaUrl, setGhaUrl]         = useState<string>('');
  const logRef     = useRef<HTMLDivElement>(null);
  const wsRef      = useRef<WebSocket | null>(null);
  const pollRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const wsLogsRef  = useRef<number>(0);  // count of lines received via WebSocket

  const { data: allScripts = [] } = useQuery<GeneratedScript[]>({
    queryKey: ['scripts'],
    queryFn:  fetchScripts,
    refetchInterval: 5000,
  });

  // Only show scripts that have been fully generated and saved to the framework repo.
  // Scripts with file_path === null are stale pre-created records where generation
  // failed or the session was closed before the file was written — they cannot be run.
  const scripts = allScripts.filter((s) => s.file_path != null && s.file_path !== '');

  const { data: runs = [], refetch: refetchRuns } = useQuery<ExecutionRun[]>({
    queryKey: ['runs'],
    queryFn:  fetchRuns,
    refetchInterval: running ? 3000 : 10000,
  });

  // Auto-scroll logs
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' });
  }, [logs]);

  // Monitor run status to detect completion
  useEffect(() => {
    if (!currentRunId) return;
    const run = runs.find((r) => r.id === currentRunId);
    if (run && ['passed', 'failed', 'error'].includes(run.status)) {
      setRunning(false);
      setRunStatus(run.status);
      if (run.status === 'passed') toast.success('Test passed ✅');
      else toast.error('Test failed ❌');
    }
  }, [runs, currentRunId]);

  // ── Start run ────────────────────────────────────────────────────────────────
  const handleRun = useCallback(async () => {
    if (!selectedScriptId) { toast.error('Select a script first'); return; }

    setLogs([]);
    setRunStatus('');
    setGhaUrl('');
    setRunning(true);
    wsLogsRef.current = 0;

    // Clear any previous HTTP poll
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }

    const { run_id } = await startRun({
      script_id:      selectedScriptId,
      environment:    params.environment,
      browser:        params.browser,
      device:         params.device,
      execution_mode: params.execution_mode,
      browser_version:params.browser_version ?? 'stable',
      tags:           params.tags.join(','),
    });

    setCurrentRunId(run_id);

    // Open WebSocket for live logs
    wsRef.current?.close();
    wsRef.current = connectRunSocket(
      run_id,
      (line) => {
        if (line === '__DONE__') {
          setRunning(false);
          refetchRuns();
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
          return;
        }
        wsLogsRef.current += 1;
        // Extract GitHub Actions URL from log lines
        const ghaMatch = line.match(/https:\/\/github\.com\/[^\s]+\/actions\/runs\/\d+/);
        if (ghaMatch) setGhaUrl(ghaMatch[0]);
        setLogs((prev) => [...prev, line]);
      },
      () => setRunning(false),
    );

    // HTTP polling fallback — if WebSocket delivers no lines within 6 s, fall back to
    // polling the Redis log history endpoint every 3 s.
    setTimeout(async () => {
      if (wsLogsRef.current > 0) return;   // WebSocket is working fine
      let lastCount = 0;
      pollRef.current = setInterval(async () => {
        try {
          const lines = await fetchRunLogs(run_id);
          if (lines.length > lastCount) {
            const newLines = lines.slice(lastCount);
            lastCount = lines.length;
            newLines.forEach((line) => {
              if (line === '__DONE__') {
                setRunning(false);
                refetchRuns();
                if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
                return;
              }
              const ghaMatch = line.match(/https:\/\/github\.com\/[^\s]+\/actions\/runs\/\d+/);
              if (ghaMatch) setGhaUrl(ghaMatch[0]);
              setLogs((prev) => [...prev, line]);
            });
          }
        } catch { /* ignore */ }
      }, 3000);
    }, 6000);

  }, [selectedScriptId, params, refetchRuns]);

  const handleStop = () => {
    wsRef.current?.close();
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    setRunning(false);
    toast('Run disconnected (process may still be running)');
  };

  // ── Helpers ──────────────────────────────────────────────────────────────────
  const setParam = <K extends keyof RunParams>(key: K, val: RunParams[K]) =>
    setParams((p) => ({ ...p, [key]: val }));

  const selectedScript = scripts.find((s) => s.id === selectedScriptId);

  // Runs table columns
  const runColumns = [
    {
      title: 'Status', width: 80,
      render: (_: unknown, r: ExecutionRun) => (
        <Space>{STATUS_ICON[r.status]} <Text>{r.status}</Text></Space>
      ),
    },
    { title: 'Env',    dataIndex: 'environment', width: 60 },
    { title: 'Browser',dataIndex: 'browser',     width: 90 },
    { title: 'Device', dataIndex: 'device',       ellipsis: true },
    {
      title: 'Tags', width: 160,
      render: (_: unknown, r: ExecutionRun) =>
        r.tags?.map((t) => <Tag key={t} color="purple">@{t}</Tag>),
    },
    {
      title: 'Started', width: 140,
      render: (_: unknown, r: ExecutionRun) =>
        r.start_time ? new Date(r.start_time).toLocaleTimeString() : '—',
    },
    {
      title: 'GHA Run', width: 80,
      render: (_: unknown, r: ExecutionRun) =>
        r.allure_report_path ? (
          <Tooltip title="Open GitHub Actions run">
            <a href={r.allure_report_path} target="_blank" rel="noreferrer">
              <LinkOutlined />
            </a>
          </Tooltip>
        ) : '—',
    },
  ];

  return (
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 120px)' }}>

      {/* ── LEFT — config panel ─────────────────────────────────────────────── */}
      <div style={{ width: 340, display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* Script selector */}
        <Card size="small" title="1. Select Generated Script">
          <Select
            style={{ width: '100%' }}
            placeholder={
              scripts.length === 0
                ? 'No ready scripts — go to AI Phase tab and generate one'
                : 'Choose a generated script…'
            }
            value={selectedScriptId || undefined}
            onChange={setSelectedScriptId}
            showSearch
            optionFilterProp="label"
          >
            {scripts.map((s) => {
              const label = s.test_script_num
                ? `${s.test_script_num} — ${(s.test_case_name ?? '').slice(0, 55)}`
                : s.file_path?.split('/').pop() ?? s.id.slice(0, 8);
              return (
                <Option key={s.id} value={s.id} label={label}>
                  <Space>
                    <Tag color={s.validation_status === 'valid' ? 'green' : 'orange'} style={{ fontSize: 11 }}>
                      {s.test_script_num ?? s.validation_status}
                    </Tag>
                    <Text style={{ fontSize: 12 }} ellipsis>
                      {(s.test_case_name ?? s.file_path?.split('/').pop() ?? s.id.slice(0, 8)).slice(0, 55)}
                    </Text>
                  </Space>
                </Option>
              );
            })}
          </Select>
          {selectedScript && (
            <div style={{ marginTop: 8, fontSize: 11, color: '#8c8c8c' }}>
              <Tag color={selectedScript.validation_status === 'valid' ? 'green' : 'orange'}>
                {selectedScript.validation_status === 'valid' ? '✓ valid' : '⚠ tsc warnings'}
              </Tag>
              {selectedScript.github_branch && (
                <Tag color="cyan">branch: {selectedScript.github_branch}</Tag>
              )}
            </div>
          )}
        </Card>

        {/* Execution parameters */}
        <Card size="small" title="2. Execution Parameters">
          <Space direction="vertical" style={{ width: '100%' }} size={10}>

            <div><Text type="secondary" style={{ fontSize: 11 }}>ENVIRONMENT</Text>
              <Select value={params.environment} style={{ width: '100%' }}
                onChange={(v) => setParam('environment', v)}>
                {ENVIRONMENTS.map((e) => (
                  <Option key={e} value={e}>{e.toUpperCase()}</Option>
                ))}
              </Select>
            </div>

            <div><Text type="secondary" style={{ fontSize: 11 }}>BROWSER</Text>
              <Select value={params.browser} style={{ width: '100%' }}
                onChange={(v) => setParam('browser', v)}>
                {BROWSERS.map((b) => <Option key={b} value={b}>{b}</Option>)}
              </Select>
            </div>

            <div><Text type="secondary" style={{ fontSize: 11 }}>DEVICE</Text>
              <Select value={params.device} style={{ width: '100%' }}
                onChange={(v) => setParam('device', v)}
                showSearch optionFilterProp="children">
                <Select.OptGroup label="Desktop">
                  {DEVICES.filter((d) => d.startsWith('Desktop')).map((d) => (
                    <Option key={d} value={d}>{d}</Option>
                  ))}
                </Select.OptGroup>
                <Select.OptGroup label="Mobile">
                  {DEVICES.filter((d) =>
                    !d.startsWith('Desktop') && !d.startsWith('iPad')
                  ).map((d) => <Option key={d} value={d}>{d}</Option>)}
                </Select.OptGroup>
                <Select.OptGroup label="Tablet">
                  {DEVICES.filter((d) => d.startsWith('iPad')).map((d) => (
                    <Option key={d} value={d}>{d}</Option>
                  ))}
                </Select.OptGroup>
              </Select>
            </div>

            <div><Text type="secondary" style={{ fontSize: 11 }}>EXECUTION MODE</Text>
              <Select value={params.execution_mode} style={{ width: '100%' }}
                onChange={(v) => setParam('execution_mode', v)}>
                {MODES.map((m) => <Option key={m} value={m}>{m}</Option>)}
              </Select>
            </div>

            <div><Text type="secondary" style={{ fontSize: 11 }}>TAGS</Text>
              <Select
                mode="multiple"
                value={params.tags}
                style={{ width: '100%' }}
                onChange={(v) => setParam('tags', v)}
              >
                {AVAILABLE_TAGS.map((t) => (
                  <Option key={t} value={t}>
                    <Tag color="purple">@{t}</Tag>
                  </Option>
                ))}
              </Select>
            </div>
          </Space>
        </Card>

        {/* Run button */}
        <Space.Compact block>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            block
            loading={running}
            disabled={!selectedScriptId}
            onClick={handleRun}
            size="large"
          >
            {running ? 'Running…' : 'Run Test'}
          </Button>
          {running && (
            <Button danger icon={<StopOutlined />} onClick={handleStop} size="large" />
          )}
        </Space.Compact>
      </div>

      {/* ── RIGHT — logs + history ──────────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* Live log terminal */}
        <Card
          size="small"
          title={
            <Space wrap>
              <span>Live Logs</span>
              {running && <Badge status="processing" text="Running" />}
              {runStatus === 'passed' && <Badge status="success" text="Passed" />}
              {runStatus === 'failed' && <Badge status="error" text="Failed" />}
              {ghaUrl && (
                <a href={ghaUrl} target="_blank" rel="noreferrer"
                   style={{ fontSize: 11, color: '#1677ff' }}>
                  <LinkOutlined /> GitHub Actions Run ↗
                </a>
              )}
            </Space>
          }
          style={{ flex: 1 }}
          bodyStyle={{ padding: 0 }}
        >
          <div
            ref={logRef}
            style={{
              background: '#0d1117',
              color: '#e6edf3',
              fontFamily: '"Cascadia Code", "Fira Code", monospace',
              fontSize: 12,
              padding: '12px 16px',
              height: 340,
              overflowY: 'auto',
              borderRadius: '0 0 6px 6px',
            }}
          >
            {logs.length === 0 && !running && (
              <span style={{ color: '#484f58' }}>
                Select a script and click "Run Test" to start…
              </span>
            )}
            {logs.map((line, i) => {
              const colour =
                line.includes('✅') || line.includes('PASSED') ? '#3fb950'
                : line.includes('❌') || line.includes('FAILED') ? '#f85149'
                : line.includes('▶') ? '#79c0ff'
                : '#e6edf3';
              return (
                <div key={i} style={{ color: colour, lineHeight: 1.6 }}>
                  {line}
                </div>
              );
            })}
            {running && (
              <span style={{ color: '#8b949e' }}>█</span>
            )}
          </div>
        </Card>

        {/* Execution history */}
        <Card size="small" title="Execution History" style={{ flex: 1, overflow: 'auto' }}>
          <Table
            dataSource={runs}
            columns={runColumns}
            rowKey="id"
            size="small"
            pagination={{ pageSize: 8, size: 'small' }}
            scroll={{ y: 220 }}
          />
        </Card>
      </div>
    </div>
  );
}
