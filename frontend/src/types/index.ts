export interface Project {
  id: string;
  name: string;
  slug: string;
  description?: string;
  icon_color: string;
  github_repo: string;
  github_token?: string;        // masked "****" in API responses
  ai_tests_branch: string;
  workflow_path?: string;
  playwright_project_path?: string;
  generated_tests_dir: string;
  runner_label: string;
  pw_host?: string;
  pw_testuser?: string;
  pw_password?: string;         // masked "****" in API responses
  pw_email?: string;
  framework_fetch_paths?: string[];
  system_prompt_override?: string;
  jira_url?: string;
  is_active: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface TestCase {
  id: string;
  project_id?: string;
  test_script_num: string;
  module: string;
  test_case_name: string;
  description: string;
  steps_count?: number;
  expected_results?: string;
  excel_source?: string;
  created_at?: string;
}

export interface GeneratedScript {
  id: string;
  project_id?: string;
  test_case_id: string;
  test_script_num?: string;   // e.g. "RB001" — joined from test_cases table
  test_case_name?: string;    // e.g. "Verify Pet landing page…"
  typescript_code?: string;
  file_path?: string;
  validation_status: 'pending' | 'valid' | 'invalid';
  validation_errors?: string;
  github_branch?: string;     // set after successful GitHub Actions run
  github_commit?: string;
  created_at: string;
}

export interface ExecutionRun {
  id: string;
  project_id?: string;
  script_id?: string;
  spec_file_path?: string;
  spec_branch?: string;
  environment: string;
  browser: string;
  device: string;
  execution_mode: string;
  run_target?: string;
  browser_version: string;
  tags: string[];
  status: 'queued' | 'running' | 'passed' | 'failed' | 'error';
  start_time?: string;
  end_time?: string;
  exit_code?: number;
  logs?: string;
  allure_report_path?: string;
}

export interface RunParams {
  environment: string;
  browser: string;
  device: string;
  execution_mode: string;
  run_target: string;        // "local" | "github_actions"
  browser_version?: string;  // kept for API compat, always 'stable' — not exposed in UI
  tags: string[];
}

export interface SpecFile {
  name: string;
  path: string;
  sha: string;
  size: number;
  branch: string;
  project_id?: string;
  repo?: string;   // "mga" for local MGA specs, undefined for GitHub-hosted specs
}
