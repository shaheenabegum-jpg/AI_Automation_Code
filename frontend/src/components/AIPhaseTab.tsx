/**
 * AI Phase Tab — Restyled with deep navy theme
 */
import { useState, useRef, useCallback, useEffect } from 'react';
import {
  Upload, Button, Table, Tag, Input, Card, Progress,
  Space, Badge, Tooltip, Typography, Radio,
} from 'antd';
import {
  UploadOutlined, ThunderboltOutlined, CheckCircleOutlined,
  CloseCircleOutlined, ReloadOutlined, CopyOutlined,
  RobotOutlined, StopOutlined, CloudUploadOutlined,
  GlobalOutlined, EyeOutlined,
} from '@ant-design/icons';
import Editor from '@monaco-editor/react';
import toast from 'react-hot-toast';
import { uploadExcel, createScriptStream, refreshFramework, fetchLLMProvider, crawlPage } from '../api/client';
import { useProjectContext } from '../context/ProjectContext';
import type { TestCase } from '../types';
import { colors, gradients } from '../theme';

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
  const { selectedProjectId, selectedProject } = useProjectContext();
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

  const [provider, setProvider]         = useState<LLMProvider>('anthropic');
  const [providerInfo, setProviderInfo] = useState<ProviderInfo | null>(null);

  // DOM Crawler state
  const [pageUrl, setPageUrl]         = useState('');
  const [crawling, setCrawling]       = useState(false);
  const [crawlResult, setCrawlResult] = useState<{
    title: string; screenshot_b64: string; element_count: number;
    elements_preview: Array<{ tag: string; selector: string; text: string }>;
  } | null>(null);

  useEffect(() => {
    fetchLLMProvider()
      .then((info: ProviderInfo) => {
        setProviderInfo(info);
        setProvider(info.default_provider);
      })
      .catch(() => {});
  }, []);

  const handleUpload = useCallback(async (file: File) => {
    setUploading(true);
    try {
      const data = await uploadExcel(file, selectedProjectId ?? undefined);
      setTestCases(data.test_cases);
      toast.success(`Parsed ${data.test_cases.length} test cases from ${file.name}`);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } }; message?: string };
      const detail = axiosErr?.response?.data?.detail ?? axiosErr?.message ?? 'Unknown error';
      toast.error(`Parse failed: ${detail}`);
    } finally {
      setUploading(false);
    }
    return false;
  }, []);

  const handleCrawl = useCallback(async () => {
    if (!pageUrl.trim()) return;
    setCrawling(true);
    setCrawlResult(null);
    try {
      const data = await crawlPage(pageUrl.trim());
      setCrawlResult(data);
      toast.success(`Crawled: ${data.title || pageUrl} — ${data.element_count} elements found`);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } }; message?: string };
      const detail = axiosErr?.response?.data?.detail ?? axiosErr?.message ?? 'Crawl failed';
      toast.error(detail);
    } finally {
      setCrawling(false);
    }
  }, [pageUrl]);

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
            setScriptCode(localCode);
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
          selectedProjectId ?? '',
          pageUrl.trim(),
        );
        stopCurrentRef.current = stop;
      });
    },
    [testCases, instruction, provider, selectedProjectId, pageUrl],
  );

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
      const passedCount = results.filter((r) => r.isValid).length;
      const failedCount = results.length - passedCount;
      if (failedCount === 0) {
        toast.success(`${passedCount} script${passedCount !== 1 ? 's' : ''} generated`);
      } else {
        toast(`${passedCount} passed  ${failedCount} failed`, { icon: '📋' });
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

  const copyScript = () => {
    navigator.clipboard.writeText(scriptCode);
    toast.success('Copied to clipboard');
  };

  const columns = [
    {
      title: 'Script #',
      dataIndex: 'test_script_num',
      width: 95,
      render: (v: string) => (
        <Tag style={{ background: `${colors.primary}22`, color: colors.primaryLight, border: 'none', borderRadius: 4 }}>
          {v}
        </Tag>
      ),
    },
    { title: 'Module', dataIndex: 'module', width: 160 },
    { title: 'Test Case', dataIndex: 'test_case_name', ellipsis: true },
    {
      title: 'Steps',
      dataIndex: 'steps_count',
      width: 65,
      render: (v: number) => <Badge count={v} style={{ backgroundColor: colors.primary }} />,
    },
  ];

  const generateButtonLabel = () => {
    if (!generating) {
      return selectedTcIds.length > 0
        ? `Generate ${selectedTcIds.length} Script${selectedTcIds.length !== 1 ? 's' : ''}`
        : 'Generate Script';
    }
    if (batchProgress) {
      return `Generating ${batchProgress.current} / ${batchProgress.total}...`;
    }
    return 'Generating...';
  };

  return (
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 130px)' }}>

      {/* LEFT PANEL */}
      <div style={{ width: 520, display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto' }}>

        {/* Active project indicator */}
        {selectedProject && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '6px 12px',
            background: `${selectedProject.icon_color}15`,
            border: `1px solid ${selectedProject.icon_color}33`,
            borderRadius: 8,
            fontSize: 12,
          }}>
            <span style={{
              width: 10, height: 10, borderRadius: '50%',
              background: selectedProject.icon_color,
              boxShadow: `0 0 6px ${selectedProject.icon_color}44`,
            }} />
            <Text style={{ color: colors.textPrimary, fontWeight: 600 }}>{selectedProject.name}</Text>
            <Text style={{ color: colors.textMuted, fontSize: 11 }}>{selectedProject.github_repo}</Text>
          </div>
        )}

        {/* 1. LLM Provider */}
        <Card
          size="small"
          className="glow-card section-card"
          title={<Space><RobotOutlined style={{ color: colors.primaryLight }} /> <span>1. LLM Provider</span></Space>}
          style={{ background: colors.bgCard, border: `1px solid ${colors.border}` }}
        >
          <Radio.Group
            value={provider}
            onChange={(e) => setProvider(e.target.value as LLMProvider)}
            buttonStyle="solid"
            style={{ width: '100%' }}
            className="llm-radio"
          >
            <Radio.Button value="anthropic" style={{ width: '50%', textAlign: 'center' }}
              disabled={providerInfo ? !providerInfo.anthropic.configured : false}>
              Anthropic Claude
            </Radio.Button>
            <Radio.Button value="gemini" style={{ width: '50%', textAlign: 'center' }}
              disabled={providerInfo ? !providerInfo.gemini.configured : false}>
              Google Gemini
            </Radio.Button>
          </Radio.Group>

          {providerInfo && (
            <div style={{ marginTop: 8, fontSize: 11 }}>
              <Text style={{ color: colors.textMuted }}>
                Model:{' '}
                <Tag style={{
                  background: provider === 'anthropic' ? `${colors.violet}22` : `${colors.info}22`,
                  color: provider === 'anthropic' ? colors.violet : colors.infoLight,
                  border: 'none',
                  fontSize: 10,
                  borderRadius: 4,
                }}>
                  {provider === 'anthropic' ? providerInfo.anthropic.model : providerInfo.gemini.model}
                </Tag>
              </Text>
              {provider === 'anthropic' && !providerInfo.anthropic.configured && (
                <div style={{ color: colors.danger, marginTop: 4 }}>ANTHROPIC_API_KEY not set</div>
              )}
              {provider === 'gemini' && !providerInfo.gemini.configured && (
                <div style={{ color: colors.danger, marginTop: 4 }}>GEMINI_API_KEY not set</div>
              )}
            </div>
          )}
        </Card>

        {/* 2. Upload */}
        <Card
          size="small"
          className="glow-card section-card"
          title={<Space><CloudUploadOutlined style={{ color: colors.primaryLight }} /> <span>2. Upload Excel</span></Space>}
          style={{ background: colors.bgCard, border: `1px solid ${colors.border}` }}
        >
          <Upload accept=".xlsx,.xls" beforeUpload={handleUpload} showUploadList={false}>
            <div className="upload-area">
              <UploadOutlined style={{ fontSize: 24, color: colors.primaryLight, marginBottom: 8 }} />
              <div style={{ color: colors.textSecondary, fontSize: 13 }}>
                {uploading ? 'Parsing...' : 'Click or drag .xlsx file here'}
              </div>
            </div>
          </Upload>
        </Card>

        {/* 3. Page URL (optional — for DOM context) */}
        <Card
          size="small"
          className="glow-card section-card"
          title={<Space><GlobalOutlined style={{ color: colors.cyan || '#22d3ee' }} /> <span>3. Page URL <Text style={{ color: colors.textMuted, fontSize: 11 }}>(optional — improves locator accuracy)</Text></span></Space>}
          style={{ background: colors.bgCard, border: `1px solid ${colors.border}` }}
        >
          <Space.Compact style={{ width: '100%' }}>
            <Input
              placeholder="https://your-app.com/page-to-test"
              value={pageUrl}
              onChange={(e) => { setPageUrl(e.target.value); setCrawlResult(null); }}
              onPressEnter={handleCrawl}
              style={{ background: colors.bgSurface, borderColor: colors.border }}
            />
            <Button
              icon={<EyeOutlined />}
              loading={crawling}
              disabled={!pageUrl.trim()}
              onClick={handleCrawl}
              style={{ borderColor: colors.border }}
            >
              Crawl
            </Button>
          </Space.Compact>

          {crawlResult && (
            <>
              <div style={{
                marginTop: 10, padding: 10, borderRadius: 8,
                background: colors.bgSurface, border: `1px solid ${colors.border}`,
                display: 'flex', gap: 12, alignItems: 'center',
              }}>
                {crawlResult.screenshot_b64 && (
                  <img
                    src={`data:image/jpeg;base64,${crawlResult.screenshot_b64}`}
                    alt="Page preview"
                    style={{ width: 160, height: 90, objectFit: 'cover', borderRadius: 6, border: `1px solid ${colors.border}` }}
                  />
                )}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: colors.textPrimary, fontWeight: 600, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {crawlResult.title || 'Untitled'}
                  </div>
                  <div style={{ color: colors.textMuted, fontSize: 11, marginTop: 2 }}>
                    {pageUrl.length > 50 ? pageUrl.slice(0, 50) + '...' : pageUrl}
                  </div>
                  <Tag color="green" style={{ marginTop: 6 }}>
                    {crawlResult.element_count} interactive elements
                  </Tag>
                </div>
              </div>

              {crawlResult.elements_preview && crawlResult.elements_preview.length > 0 && (
                <details style={{ marginTop: 8 }}>
                  <summary style={{
                    cursor: 'pointer', color: colors.textSecondary, fontSize: 11,
                    userSelect: 'none', padding: '4px 0',
                  }}>
                    Show {Math.min(crawlResult.elements_preview.length, 20)} elements
                  </summary>
                  <div style={{
                    maxHeight: 200, overflowY: 'auto', marginTop: 6,
                    background: colors.bgDeepest || colors.bgSurface,
                    borderRadius: 6, padding: 8,
                    border: `1px solid ${colors.border}`,
                    fontFamily: 'monospace', fontSize: 11, lineHeight: 1.6,
                  }}>
                    {crawlResult.elements_preview.slice(0, 20).map((el: { tag: string; selector: string; text: string }, i: number) => (
                      <div key={i} style={{ color: colors.textSecondary, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        <span style={{ color: '#22d3ee', fontWeight: 600 }}>{el.tag}</span>
                        {el.selector && <span style={{ color: colors.textMuted }}> {el.selector.slice(0, 50)}</span>}
                        {el.text && <span style={{ color: '#94a3b8' }}> &quot;{el.text.slice(0, 30)}&quot;</span>}
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </>
          )}
        </Card>

        {/* 4. Test cases */}
        {testCases.length > 0 && (
          <Card
            size="small"
            className="glow-card section-card"
            title={
              <Space>
                <span>4. Select Test Cases</span>
                {selectedTcIds.length > 0 && (
                  <Tag style={{ background: `${colors.primary}22`, color: colors.primaryLight, border: 'none', borderRadius: 4 }}>
                    {selectedTcIds.length} selected
                  </Tag>
                )}
                <Text style={{ fontSize: 11, color: colors.textMuted }}>({testCases.length} loaded)</Text>
              </Space>
            }
            bodyStyle={{ padding: 0 }}
            style={{ background: colors.bgCard, border: `1px solid ${colors.border}` }}
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

        {/* 4. Instructions */}
        <Card
          size="small"
          className="glow-card section-card"
          title={<span>5. Extra Instructions <Text style={{ color: colors.textMuted, fontSize: 11 }}>(optional)</Text></span>}
          style={{ background: colors.bgCard, border: `1px solid ${colors.border}` }}
        >
          <TextArea
            rows={3}
            placeholder="e.g. Add mobile viewport assertions. Use data-testid selectors where possible."
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            style={{ background: colors.bgSurface, borderColor: colors.border }}
          />
        </Card>

        {/* Progress */}
        {batchProgress && (
          <Progress
            percent={Math.round((batchProgress.current / batchProgress.total) * 100)}
            format={() => `${batchProgress.current} / ${batchProgress.total}`}
            strokeColor={{ from: colors.primary, to: colors.purple }}
            trailColor={colors.bgElevated}
            size="small"
          />
        )}

        {/* Generate / Stop / Refresh */}
        <Space.Compact block>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            onClick={handleGenerate}
            loading={generating}
            disabled={selectedTcIds.length === 0}
            className="gradient-btn"
            style={{ flex: 1 }}
            size="large"
          >
            {generateButtonLabel()}
          </Button>
          {generating && (
            <Button danger icon={<StopOutlined />} onClick={handleStop} size="large" />
          )}
          <Tooltip title="Re-fetch framework repo from GitHub">
            <Button
              icon={<ReloadOutlined />}
              loading={refreshing}
              onClick={handleRefresh}
              size="large"
            />
          </Tooltip>
        </Space.Compact>

        {/* Batch results */}
        {batchResults.length > 0 && (
          <Card
            size="small"
            className="glow-card section-card"
            title="Generation Results"
            bodyStyle={{ padding: '8px 12px' }}
            style={{ background: colors.bgCard, border: `1px solid ${colors.border}` }}
          >
            {batchResults.map((r) => (
              <div
                key={r.tcId}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 0',
                  borderBottom: `1px solid ${colors.border}`,
                  fontSize: 12,
                }}
              >
                {r.isValid
                  ? <CheckCircleOutlined style={{ color: colors.success, flexShrink: 0 }} />
                  : <CloseCircleOutlined style={{ color: colors.danger, flexShrink: 0 }} />}
                <Tag style={{ background: `${colors.primary}22`, color: colors.primaryLight, border: 'none', fontSize: 10, flexShrink: 0, borderRadius: 4 }}>
                  {r.tcNum}
                </Tag>
                <Text ellipsis={{ tooltip: r.tcName }} style={{ flex: 1, fontSize: 11 }}>
                  {r.tcName}
                </Text>
                <Tag
                  style={{
                    background: r.isValid ? `${colors.success}22` : `${colors.danger}22`,
                    color: r.isValid ? colors.success : colors.danger,
                    border: 'none',
                    fontSize: 10,
                    flexShrink: 0,
                    borderRadius: 4,
                  }}
                >
                  {r.isValid ? 'valid' : 'invalid'}
                </Tag>
              </div>
            ))}
          </Card>
        )}
      </div>

      {/* RIGHT PANEL — Monaco editor */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Text strong style={{ fontSize: 14 }}>Generated TypeScript / Playwright</Text>
          {scriptCode && (
            <Button size="small" icon={<CopyOutlined />} onClick={copyScript}
              style={{ borderColor: colors.border }}>
              Copy
            </Button>
          )}
        </div>
        <div className={`editor-wrapper ${generating ? 'is-generating' : ''}`} style={{ flex: 1 }}>
          <Editor
            height="100%"
            language="typescript"
            theme="vs-dark"
            value={scriptCode || '// Generated script will appear here...'}
            options={{
              readOnly: generating,
              minimap: { enabled: false },
              fontSize: 13,
              scrollBeyondLastLine: false,
              wordWrap: 'on',
              padding: { top: 12 },
            }}
          />
        </div>
        {generating && batchProgress && (
          <div style={{
            color: colors.success,
            fontFamily: '"Cascadia Code", "Fira Code", monospace',
            fontSize: 12,
            padding: '4px 0',
          }}>
            Script {batchProgress.current} / {batchProgress.total} — streaming from{' '}
            {provider === 'gemini' ? 'Gemini' : 'Claude'}...
          </div>
        )}
      </div>
    </div>
  );
}
