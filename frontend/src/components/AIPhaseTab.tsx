/**
 * AI Phase Tab
 * ============
 * 1. Upload .xlsx  →  preview parsed test cases
 * 2. Choose LLM provider (Anthropic Claude / Google Gemini)
 * 3. Select one or more test cases (checkboxes), optionally write extra instructions
 * 4. Click "Generate N Scripts" — runs sequentially, showing live progress
 * 5. Monaco editor shows the last generated script in real time
 * 6. Batch summary card shows ✅/❌ per test case after completion
 */
import { useState, useRef, useCallback, useEffect } from 'react';
import {
  Upload, Button, Table, Tag, Input, Card, Progress,
  Space, Badge, Tooltip, Typography, Radio,
} from 'antd';
import {
  UploadOutlined, ThunderboltOutlined, CheckCircleOutlined,
  CloseCircleOutlined, ReloadOutlined, CopyOutlined,
  RobotOutlined, StopOutlined,
} from '@ant-design/icons';
import Editor from '@monaco-editor/react';
import toast from 'react-hot-toast';
import { uploadExcel, createScriptStream, refreshFramework, fetchLLMProvider } from '../api/client';
import type { TestCase } from '../types';

type LLMProvider = 'anthropic' | 'gemini';

interface ProviderInfo {
  default_provider: LLMProvider;
  anthropic: { model: string; configured: boolean };
  gemini:    { model: string; configured: boolean };
}

const { TextArea } = Input;
const { Text } = Typography;

interface BatchResult {
  tcId:     string;
  tcNum:    string;
  tcName:   string;
  scriptId: string;
  isValid:  boolean;
  errors:   string;
}

export default function AIPhaseTab() {
  const [testCases, setTestCases]           = useState<TestCase[]>([]);
  const [selectedTcIds, setSelectedTcIds]   = useState<React.Key[]>([]);
  const [instruction, setInstruction]       = useState('');
  const [scriptCode, setScriptCode]         = useState('');
  const [generating, setGenerating]         = useState(false);
  const [batchProgress, setBatchProgress]   = useState<{ current: number; total: number } | null>(null);
  const [batchResults, setBatchResults]     = useState<BatchResult[]>([]);
  const [uploading, setUploading]           = useState(false);
  const [refreshing, setRefreshing]         = useState(false);
  const abortRef       = useRef<boolean>(false);
  const stopCurrentRef = useRef<(() => void) | null>(null);

  // ── LLM Provider ────────────────────────────────────────────────────────────
  const [provider, setProvider]         = useState<LLMProvider>('anthropic');
  const [providerInfo, setProviderInfo] = useState<ProviderInfo | null>(null);

  useEffect(() => {
    fetchLLMProvider()
      .then((info: ProviderInfo) => {
        setProviderInfo(info);
        setProvider(info.default_provider);
      })
      .catch(() => { /* silently ignore if backend not up yet */ });
  }, []);

  // ── Excel upload ─────────────────────────────────────────────────────────────
  const handleUpload = useCallback(async (file: File) => {
    setUploading(true);
    try {
      const data = await uploadExcel(file);
      setTestCases(data.test_cases);
      toast.success(`Parsed ${data.test_cases.length} test cases from ${file.name}`);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } }; message?: string };
      const detail = axiosErr?.response?.data?.detail ?? axiosErr?.message ?? 'Unknown error';
      toast.error(`Parse failed: ${detail}`);
    } finally {
      setUploading(false);
    }
    return false; // prevent antd default upload behaviour
  }, []);

  // ── Generate one script — returns a Promise ──────────────────────────────────
  const generateOne = useCallback(
    (tcId: string): Promise<BatchResult> => {
      return new Promise((resolve) => {
        const tc = testCases.find((t) => t.id === tcId);
        let localCode = '';

        const stop = createScriptStream(
          tcId,
          instruction,
          (chunk) => {
            localCode += chunk;
            setScriptCode(localCode); // stream into Monaco in real time
          },
          (scriptId, isValid, errors) => {
            resolve({
              tcId,
              tcNum:  tc?.test_script_num ?? tcId.slice(0, 8),
              tcName: tc?.test_case_name  ?? 'Unknown',
              scriptId,
              isValid,
              errors,
            });
          },
          (msg) => {
            resolve({
              tcId,
              tcNum:  tc?.test_script_num ?? tcId.slice(0, 8),
              tcName: tc?.test_case_name  ?? 'Unknown',
              scriptId: '',
              isValid: false,
              errors: msg,
            });
          },
          provider,
        );
        stopCurrentRef.current = stop;
      });
    },
    [testCases, instruction, provider],
  );

  // ── Batch generate — loops sequentially ─────────────────────────────────────
  const handleGenerate = async () => {
    if (selectedTcIds.length === 0) {
      toast.error('Select at least one test case');
      return;
    }

    abortRef.current = false;
    setGenerating(true);
    setBatchResults([]);
    setScriptCode('');

    const results: BatchResult[] = [];
    const ids = selectedTcIds as string[];

    for (let i = 0; i < ids.length; i++) {
      if (abortRef.current) break;
      setBatchProgress({ current: i + 1, total: ids.length });
      const res = await generateOne(ids[i]);
      results.push(res);
      setBatchResults([...results]);
    }

    setGenerating(false);
    setBatchProgress(null);

    if (!abortRef.current) {
      const passed = results.filter((r) => r.isValid).length;
      const failed = results.length - passed;
      if (failed === 0) {
        toast.success(`${passed} script${passed !== 1 ? 's' : ''} generated ✅`);
      } else {
        toast(`${passed} ✅  ${failed} ❌ — see results below`, { icon: '📋' });
      }
    } else {
      toast('Generation stopped');
    }
  };

  const handleStop = () => {
    abortRef.current = true;
    stopCurrentRef.current?.();
    setGenerating(false);
    setBatchProgress(null);
  };

  // ── Framework refresh ────────────────────────────────────────────────────────
  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const res = await refreshFramework();
      toast.success(`Framework context refreshed (${res.chars} chars)`);
    } catch {
      toast.error('Framework refresh failed');
    } finally {
      setRefreshing(false);
    }
  };

  // ── Copy to clipboard ────────────────────────────────────────────────────────
  const copyScript = () => {
    navigator.clipboard.writeText(scriptCode);
    toast.success('Copied to clipboard');
  };

  // ── Table columns ────────────────────────────────────────────────────────────
  const columns = [
    {
      title: 'Script #',
      dataIndex: 'test_script_num',
      width: 95,
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    { title: 'Module', dataIndex: 'module', width: 160 },
    { title: 'Test Case', dataIndex: 'test_case_name', ellipsis: true },
    {
      title: 'Steps',
      dataIndex: 'steps_count',
      width: 65,
      render: (v: number) => <Badge count={v} color="geekblue" />,
    },
  ];

  // ── Dynamic button label ─────────────────────────────────────────────────────
  const generateButtonLabel = () => {
    if (!generating) {
      return selectedTcIds.length > 0
        ? `Generate ${selectedTcIds.length} Script${selectedTcIds.length !== 1 ? 's' : ''}`
        : 'Generate Script';
    }
    if (batchProgress) {
      return `Generating ${batchProgress.current} / ${batchProgress.total}…`;
    }
    return 'Generating…';
  };

  return (
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 120px)' }}>
      {/* ── LEFT PANEL ─────────────────────────────────────────────────────── */}
      <div style={{ width: 520, display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto' }}>

        {/* 1. LLM Provider selector */}
        <Card
          size="small"
          title={
            <Space>
              <RobotOutlined />
              <span>1. LLM Provider</span>
            </Space>
          }
        >
          <Radio.Group
            value={provider}
            onChange={(e) => setProvider(e.target.value as LLMProvider)}
            buttonStyle="solid"
            style={{ width: '100%' }}
          >
            <Radio.Button
              value="anthropic"
              style={{ width: '50%', textAlign: 'center' }}
              disabled={providerInfo ? !providerInfo.anthropic.configured : false}
            >
              🤖 Anthropic
            </Radio.Button>
            <Radio.Button
              value="gemini"
              style={{ width: '50%', textAlign: 'center' }}
              disabled={providerInfo ? !providerInfo.gemini.configured : false}
            >
              ✨ Gemini
            </Radio.Button>
          </Radio.Group>

          {providerInfo && (
            <div style={{ marginTop: 8, fontSize: 11 }}>
              <Text type="secondary">
                Model:{' '}
                <Tag color={provider === 'anthropic' ? 'purple' : 'blue'} style={{ fontSize: 10 }}>
                  {provider === 'anthropic'
                    ? providerInfo.anthropic.model
                    : providerInfo.gemini.model}
                </Tag>
              </Text>
              {provider === 'anthropic' && !providerInfo.anthropic.configured && (
                <div style={{ color: '#ff4d4f', marginTop: 4 }}>
                  ⚠ ANTHROPIC_API_KEY not set in .env
                </div>
              )}
              {provider === 'gemini' && !providerInfo.gemini.configured && (
                <div style={{ color: '#ff4d4f', marginTop: 4 }}>
                  ⚠ GEMINI_API_KEY not set in .env
                </div>
              )}
            </div>
          )}
        </Card>

        {/* 2. Upload */}
        <Card size="small" title="2. Upload Excel">
          <Upload
            accept=".xlsx,.xls"
            beforeUpload={handleUpload}
            showUploadList={false}
          >
            <Button icon={<UploadOutlined />} loading={uploading} block>
              {uploading ? 'Parsing…' : 'Upload file.xlsx'}
            </Button>
          </Upload>
        </Card>

        {/* 3. Test case list with checkboxes */}
        {testCases.length > 0 && (
          <Card
            size="small"
            title={
              <Space>
                <span>3. Select Test Cases</span>
                {selectedTcIds.length > 0 && (
                  <Tag color="blue">{selectedTcIds.length} selected</Tag>
                )}
                <Text type="secondary" style={{ fontSize: 11 }}>
                  ({testCases.length} loaded)
                </Text>
              </Space>
            }
            bodyStyle={{ padding: 0 }}
          >
            <Table
              dataSource={testCases}
              columns={columns}
              rowKey="id"
              size="small"
              pagination={false}
              scroll={{ y: 220 }}
              rowSelection={{
                type: 'checkbox',
                selectedRowKeys: selectedTcIds,
                onChange: setSelectedTcIds,
              }}
            />
          </Card>
        )}

        {/* 4. Instruction */}
        <Card size="small" title="4. Extra Instructions (optional)">
          <TextArea
            rows={3}
            placeholder="e.g. Add mobile viewport assertions. Use data-testid selectors where possible."
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
          />
        </Card>

        {/* Progress bar — visible only during batch */}
        {batchProgress && (
          <Progress
            percent={Math.round((batchProgress.current / batchProgress.total) * 100)}
            format={() => `${batchProgress.current} / ${batchProgress.total}`}
            strokeColor={{ from: '#108ee9', to: '#87d068' }}
            size="small"
          />
        )}

        {/* Generate / Stop / Refresh controls */}
        <Space.Compact block>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            onClick={handleGenerate}
            loading={generating}
            disabled={selectedTcIds.length === 0}
            style={{ flex: 1 }}
          >
            {generateButtonLabel()}
          </Button>
          {generating && (
            <Button danger icon={<StopOutlined />} onClick={handleStop}>
              Stop
            </Button>
          )}
          <Tooltip title="Re-fetch framework repo from GitHub">
            <Button
              icon={<ReloadOutlined />}
              loading={refreshing}
              onClick={handleRefresh}
            />
          </Tooltip>
        </Space.Compact>

        {/* Batch results summary */}
        {batchResults.length > 0 && (
          <Card size="small" title="Generation Results" bodyStyle={{ padding: '8px 12px' }}>
            {batchResults.map((r) => (
              <div
                key={r.tcId}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '4px 0',
                  borderBottom: '1px solid #1f1f1f',
                  fontSize: 12,
                }}
              >
                {r.isValid
                  ? <CheckCircleOutlined style={{ color: '#52c41a', flexShrink: 0 }} />
                  : <CloseCircleOutlined style={{ color: '#ff4d4f', flexShrink: 0 }} />}
                <Tag color="blue" style={{ fontSize: 10, flexShrink: 0 }}>{r.tcNum}</Tag>
                <Text ellipsis={{ tooltip: r.tcName }} style={{ flex: 1, fontSize: 11 }}>
                  {r.tcName}
                </Text>
                <Tag color={r.isValid ? 'success' : 'error'} style={{ fontSize: 10, flexShrink: 0 }}>
                  {r.isValid ? 'valid' : 'invalid'}
                </Tag>
              </div>
            ))}
          </Card>
        )}
      </div>

      {/* ── RIGHT PANEL — Monaco editor ──────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Text strong>Generated TypeScript / Playwright</Text>
          {scriptCode && (
            <Button size="small" icon={<CopyOutlined />} onClick={copyScript}>
              Copy
            </Button>
          )}
        </div>
        <div style={{
          flex: 1, border: '1px solid #303030', borderRadius: 6, overflow: 'hidden',
        }}>
          <Editor
            height="100%"
            language="typescript"
            theme="vs-dark"
            value={scriptCode || '// Generated script will appear here…'}
            options={{
              readOnly: generating,
              minimap: { enabled: false },
              fontSize: 13,
              scrollBeyondLastLine: false,
              wordWrap: 'on',
            }}
          />
        </div>
        {generating && batchProgress && (
          <div style={{ color: '#52c41a', fontFamily: 'monospace', fontSize: 12 }}>
            ▶ Script {batchProgress.current} / {batchProgress.total} — streaming from{' '}
            {provider === 'gemini' ? 'Gemini' : 'Claude'}…
          </div>
        )}
        {generating && !batchProgress && (
          <div style={{ color: '#52c41a', fontFamily: 'monospace', fontSize: 12 }}>
            ▶ Streaming from {provider === 'gemini' ? 'Gemini' : 'Claude'}…
          </div>
        )}
      </div>
    </div>
  );
}
