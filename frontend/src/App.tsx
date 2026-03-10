import { Suspense, lazy } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider, Layout, Tabs, theme, Spin } from 'antd';
import {
  DashboardOutlined, RobotOutlined, PlaySquareOutlined,
} from '@ant-design/icons';
import { Toaster } from 'react-hot-toast';

const Dashboard  = lazy(() => import('./components/Dashboard'));
const AIPhaseTab = lazy(() => import('./components/AIPhaseTab'));
const RunTab     = lazy(() => import('./components/RunTab'));

const { Header, Content } = Layout;
const qc = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}>
        <Toaster position="top-right" toastOptions={{ style: { background: '#1f1f1f', color: '#fff' } }} />
        <Layout style={{ minHeight: '100vh', background: '#0d0d0d' }}>

          {/* Header */}
          <Header style={{
            background: '#141414',
            borderBottom: '1px solid #1f1f1f',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            padding: '0 24px',
          }}>
            <RobotOutlined style={{ fontSize: 22, color: '#1677ff' }} />
            <span style={{ fontWeight: 700, fontSize: 16, color: '#fff' }}>
              AI Test Automation Platform
            </span>
            <span style={{ marginLeft: 'auto', color: '#8b949e', fontSize: 12 }}>
              AI_SDET_V:2.0
            </span>
          </Header>

          {/* Main content */}
          <Content style={{ padding: '16px 24px' }}>
            <Tabs
              defaultActiveKey="dashboard"
              size="large"
              items={[
                {
                  key:      'dashboard',
                  label:    <><DashboardOutlined /> Dashboard</>,
                  children: (
                    <Suspense fallback={<Spin />}>
                      <Dashboard />
                    </Suspense>
                  ),
                },
                {
                  key:      'ai-phase',
                  label:    <><RobotOutlined /> AI Phase</>,
                  children: (
                    <Suspense fallback={<Spin />}>
                      <AIPhaseTab />
                    </Suspense>
                  ),
                },
                {
                  key:      'run',
                  label:    <><PlaySquareOutlined /> Run Testcase</>,
                  children: (
                    <Suspense fallback={<Spin />}>
                      <RunTab />
                    </Suspense>
                  ),
                },
              ]}
            />
          </Content>
        </Layout>
      </ConfigProvider>
    </QueryClientProvider>
  );
}
