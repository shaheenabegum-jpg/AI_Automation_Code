/**
 * Dashboard Tab
 * =============
 * - Pass/Fail pie chart from execution history
 * - Latest runs table with Allure links
 * - Script library with validation badge
 * - Allure report embed (iframe) for the latest passed run
 */
import { useState } from 'react';
import {
  Card, Table, Tag, Space, Badge, Typography,
  Statistic, Row, Col, Button, Tabs, Empty,
} from 'antd';
import {
  CheckCircleFilled, CloseCircleFilled, ClockCircleFilled,
  BarChartOutlined, FileTextOutlined, LinkOutlined,
} from '@ant-design/icons';
import {
  PieChart, Pie, Cell, Tooltip as RTooltip, Legend, ResponsiveContainer,
} from 'recharts';
import { useQuery } from '@tanstack/react-query';
import { fetchRuns, fetchScripts } from '../api/client';
import type { ExecutionRun, GeneratedScript } from '../types';

const { Text, Title } = Typography;

const STATUS_COLOR: Record<string, string> = {
  passed:  '#52c41a',
  failed:  '#ff4d4f',
  running: '#1677ff',
  queued:  '#faad14',
  error:   '#ff7875',
};

export default function Dashboard() {
  const [allureRunId, setAllureRunId] = useState('');

  const { data: runs = [] } = useQuery<ExecutionRun[]>({
    queryKey: ['runs'],
    queryFn:  fetchRuns,
    refetchInterval: 15_000,
  });

  const { data: scripts = [] } = useQuery<GeneratedScript[]>({
    queryKey: ['scripts'],
    queryFn:  fetchScripts,
    refetchInterval: 15_000,
  });

  // ── Stats ───────────────────────────────────────────────────────────────────
  const passed  = runs.filter((r) => r.status === 'passed').length;
  const failed  = runs.filter((r) => r.status === 'failed').length;
  const running = runs.filter((r) => r.status === 'running').length;
  const total   = runs.length;

  const pieData = [
    { name: 'Passed',  value: passed,  color: '#52c41a' },
    { name: 'Failed',  value: failed,  color: '#ff4d4f' },
    { name: 'Running', value: running, color: '#1677ff' },
  ].filter((d) => d.value > 0);

  // ── Run table columns ────────────────────────────────────────────────────────
  const runColumns = [
    {
      title: 'Status', width: 100,
      render: (_: unknown, r: ExecutionRun) => (
        <Tag color={STATUS_COLOR[r.status]} icon={
          r.status === 'passed'  ? <CheckCircleFilled /> :
          r.status === 'failed'  ? <CloseCircleFilled /> :
          <ClockCircleFilled />
        }>
          {r.status.toUpperCase()}
        </Tag>
      ),
    },
    { title: 'Env',    dataIndex: 'environment', width: 60 },
    { title: 'Browser',dataIndex: 'browser',     width: 90 },
    { title: 'Device', dataIndex: 'device',       ellipsis: true },
    {
      title: 'Mode', dataIndex: 'execution_mode', width: 90,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: 'Tags', width: 180,
      render: (_: unknown, r: ExecutionRun) =>
        r.tags?.map((t) => <Tag key={t} color="purple">@{t}</Tag>),
    },
    {
      title: 'Started', width: 130,
      render: (_: unknown, r: ExecutionRun) =>
        r.start_time ? new Date(r.start_time).toLocaleString() : '—',
    },
    {
      title: 'Duration', width: 90,
      render: (_: unknown, r: ExecutionRun) => {
        if (!r.start_time || !r.end_time) return '—';
        const ms = new Date(r.end_time).getTime() - new Date(r.start_time).getTime();
        return `${(ms / 1000).toFixed(1)}s`;
      },
    },
    {
      title: 'Report', width: 80,
      render: (_: unknown, r: ExecutionRun) =>
        r.allure_report_path ? (
          <Space>
            <Button
              size="small"
              icon={<LinkOutlined />}
              href={`http://localhost:8000/api/reports/${r.id}`}
              target="_blank"
            >
              Open
            </Button>
            <Button
              size="small"
              onClick={() => setAllureRunId(r.id)}
            >
              Embed
            </Button>
          </Space>
        ) : '—',
    },
  ];

  // ── Script table columns ─────────────────────────────────────────────────────
  const scriptColumns = [
    {
      title: 'File', dataIndex: 'file_path', ellipsis: true,
      render: (v: string) => v?.split('/').pop() ?? '—',
    },
    {
      title: 'Validation', dataIndex: 'validation_status', width: 110,
      render: (v: string) => (
        <Badge
          status={v === 'valid' ? 'success' : v === 'invalid' ? 'error' : 'default'}
          text={v}
        />
      ),
    },
    {
      title: 'Created', width: 130,
      render: (_: unknown, s: GeneratedScript) =>
        new Date(s.created_at).toLocaleString(),
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

      {/* ── Stats row ─────────────────────────────────────────────────────────── */}
      <Row gutter={16}>
        {[
          { title: 'Total Runs',      value: total,   color: '#1677ff' },
          { title: 'Passed',          value: passed,  color: '#52c41a' },
          { title: 'Failed',          value: failed,  color: '#ff4d4f' },
          { title: 'Scripts Generated', value: scripts.length, color: '#722ed1' },
        ].map(({ title, value, color }) => (
          <Col span={6} key={title}>
            <Card size="small">
              <Statistic
                title={title}
                value={value}
                valueStyle={{ color, fontSize: 28 }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      {/* ── Chart + runs table ──────────────────────────────────────────────── */}
      <Row gutter={16}>
        <Col span={7}>
          <Card
            size="small"
            title={<><BarChartOutlined /> Pass / Fail Distribution</>}
            style={{ height: 280 }}
          >
            {pieData.length > 0 ? (
              <ResponsiveContainer width="100%" height={220}>
                <PieChart>
                  <Pie
                    data={pieData}
                    cx="50%"
                    cy="50%"
                    innerRadius={55}
                    outerRadius={85}
                    paddingAngle={3}
                    dataKey="value"
                    label={({ name, percent }) =>
                      `${name} ${(percent * 100).toFixed(0)}%`
                    }
                  >
                    {pieData.map((entry, i) => (
                      <Cell key={i} fill={entry.color} />
                    ))}
                  </Pie>
                  <RTooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <Empty description="No runs yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Card>
        </Col>

        <Col span={17}>
          <Card
            size="small"
            title={<><FileTextOutlined /> Script Library</>}
            style={{ height: 280 }}
          >
            <Table
              dataSource={scripts}
              columns={scriptColumns}
              rowKey="id"
              size="small"
              pagination={{ pageSize: 5, size: 'small' }}
              scroll={{ y: 170 }}
            />
          </Card>
        </Col>
      </Row>

      {/* ── Execution history ─────────────────────────────────────────────────── */}
      <Card size="small" title="Execution History">
        <Table
          dataSource={runs}
          columns={runColumns}
          rowKey="id"
          size="small"
          pagination={{ pageSize: 8, size: 'small' }}
          scroll={{ x: 1100 }}
        />
      </Card>

      {/* ── Allure report embed ─────────────────────────────────────────────── */}
      {allureRunId && (
        <Card
          size="small"
          title="Allure Report"
          extra={
            <Button size="small" onClick={() => setAllureRunId('')}>Close</Button>
          }
        >
          <iframe
            src={`http://localhost:8000/api/reports/${allureRunId}`}
            style={{ width: '100%', height: 600, border: 'none' }}
            title="Allure Report"
          />
        </Card>
      )}
    </div>
  );
}
