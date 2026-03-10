export interface TestCase {
  id: string;
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
  script_id: string;
  environment: string;
  browser: string;
  device: string;
  execution_mode: string;
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
  browser_version?: string;  // kept for API compat, always 'stable' — not exposed in UI
  tags: string[];
}
