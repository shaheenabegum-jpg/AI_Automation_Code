import { Suspense, lazy } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider, Layout, Tabs, theme, Spin } from 'antd';
import {
  DashboardOutlined, RobotOutlined, PlaySquareOutlined, AppstoreOutlined,
  SunOutlined, MoonOutlined,
} from '@ant-design/icons';
import { Toaster } from 'react-hot-toast';
import { colors, gradients, getAntThemeTokens, getAntComponentTokens } from './theme';
import { ProjectProvider } from './context/ProjectContext';
import { ThemeProvider, useThemeContext } from './context/ThemeContext';
import ProjectSelector from './components/ProjectSelector';

const Dashboard   = lazy(() => import('./components/Dashboard'));
const AIPhaseTab  = lazy(() => import('./components/AIPhaseTab'));
const RunTab      = lazy(() => import('./components/RunTab'));
const ProjectsTab = lazy(() => import('./components/ProjectsTab'));

const { Header, Content } = Layout;
const qc = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });

/** Inner app that reads theme context */
function AppContent() {
  const { mode, isDark, toggleTheme } = useThemeContext();

  return (
    <ConfigProvider
      theme={{
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: getAntThemeTokens(mode),
        components: getAntComponentTokens(mode),
      }}
    >
      <Toaster
        position="top-right"
        toastOptions={{
          style: {
            background: colors.bgCard,
            color: colors.textPrimary,
            border: `1px solid ${colors.border}`,
            borderRadius: 8,
          },
        }}
      />

      <Layout style={{ minHeight: '100vh', background: colors.bgDeepest }}>

        {/* Animated gradient accent bar */}
        <div className="header-accent-bar" />

        {/* Header */}
        <Header style={{
          background: gradients.header,
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          alignItems: 'center',
          gap: 14,
          padding: '0 28px',
          height: 56,
        }}>
          <div style={{
            width: 36, height: 36,
            borderRadius: 10,
            background: gradients.primary,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            boxShadow: '0 0 16px rgba(99, 102, 241, 0.3)',
          }}>
            <RobotOutlined style={{ fontSize: 20, color: '#fff' }} />
          </div>
          <span style={{
            fontWeight: 700,
            fontSize: 17,
            color: colors.textPrimary,
            letterSpacing: '-0.01em',
          }}>
            AI Test Automation Platform
          </span>

          {/* Right side of header */}
          <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 12 }}>
            <ProjectSelector />

            {/* Theme toggle */}
            <button
              className="theme-toggle-btn"
              onClick={toggleTheme}
              title={isDark ? 'Switch to Light Mode' : 'Switch to Dark Mode'}
            >
              {isDark ? <SunOutlined /> : <MoonOutlined />}
            </button>

            <span className="version-pill">AI_SDET v2.0</span>
          </span>
        </Header>

        {/* Main content */}
        <Content style={{ padding: '16px 28px' }}>
          <Tabs
            defaultActiveKey="dashboard"
            size="large"
            className="main-tabs"
            items={[
              {
                key:   'dashboard',
                label: (
                  <span className="tab-label tab-dashboard">
                    <span className="tab-icon-wrap tab-icon-dashboard">
                      <DashboardOutlined />
                    </span>
                    Dashboard
                  </span>
                ),
                children: (
                  <Suspense fallback={<Spin size="large" style={{ display: 'block', margin: '80px auto' }} />}>
                    <Dashboard />
                  </Suspense>
                ),
              },
              {
                key:   'ai-phase',
                label: (
                  <span className="tab-label tab-ai-phase">
                    <span className="tab-icon-wrap tab-icon-ai">
                      <RobotOutlined />
                    </span>
                    AI Phase
                  </span>
                ),
                children: (
                  <Suspense fallback={<Spin size="large" style={{ display: 'block', margin: '80px auto' }} />}>
                    <AIPhaseTab />
                  </Suspense>
                ),
              },
              {
                key:   'run',
                label: (
                  <span className="tab-label tab-run">
                    <span className="tab-icon-wrap tab-icon-run">
                      <PlaySquareOutlined />
                    </span>
                    Run Testcase
                  </span>
                ),
                children: (
                  <Suspense fallback={<Spin size="large" style={{ display: 'block', margin: '80px auto' }} />}>
                    <RunTab />
                  </Suspense>
                ),
              },
              {
                key:   'projects',
                label: (
                  <span className="tab-label tab-projects">
                    <span className="tab-icon-wrap tab-icon-projects">
                      <AppstoreOutlined />
                    </span>
                    Projects
                  </span>
                ),
                children: (
                  <Suspense fallback={<Spin size="large" style={{ display: 'block', margin: '80px auto' }} />}>
                    <ProjectsTab />
                  </Suspense>
                ),
              },
            ]}
          />
        </Content>
      </Layout>
    </ConfigProvider>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <ProjectProvider>
          <AppContent />
        </ProjectProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
