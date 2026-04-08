import axios from 'axios';
import type { Project, MCPConfig, MCPStep, MCPEvent } from '../types';

// Use relative URLs so requests go through the Vite dev proxy → avoids CORS entirely.
// Vite config: '/api' proxied to http://localhost:8000, '/ws' proxied to ws://localhost:8000
const BASE_URL = '';

export const api = axios.create({ baseURL: BASE_URL });


// ── Projects ─────────────────────────────────────────────────────────────────
export const fetchProjects = async (): Promise<Project[]> => {
  const { data } = await api.get('/api/projects');
  return data;
};

export const createProject = async (project: Partial<Project>): Promise<Project> => {
  const { data } = await api.post('/api/projects', project);
  return data;
};

export const updateProject = async (id: string, project: Partial<Project>): Promise<Project> => {
  const { data } = await api.put(`/api/projects/${id}`, project);
  return data;
};

export const deleteProject = async (id: string) => {
  const { data } = await api.delete(`/api/projects/${id}`);
  return data;
};


// ── Excel Upload ──────────────────────────────────────────────────────────────
export const uploadExcel = async (file: File, projectId?: string) => {
  const fd = new FormData();
  fd.append('file', file);
  if (projectId) fd.append('project_id', projectId);
  const { data } = await api.post('/api/parse-excel', fd);
  return data;
};

// ── Test Cases ────────────────────────────────────────────────────────────────
export const fetchTestCases = async (projectId?: string) => {
  const params: Record<string, string> = {};
  if (projectId) params.project_id = projectId;
  const { data } = await api.get('/api/test-cases', { params });
  return data;
};

// ── Scripts ───────────────────────────────────────────────────────────────────
export const fetchScripts = async (projectId?: string) => {
  const params: Record<string, string> = {};
  if (projectId) params.project_id = projectId;
  const { data } = await api.get('/api/scripts', { params });
  return data;
};

export const fetchScript = async (id: string) => {
  const { data } = await api.get(`/api/scripts/${id}`);
  return data;
};

// ── Runs ──────────────────────────────────────────────────────────────────────
export const fetchRuns = async (projectId?: string) => {
  const params: Record<string, string> = {};
  if (projectId) params.project_id = projectId;
  const { data } = await api.get('/api/runs', { params });
  return data;
};

export const fetchRun = async (id: string) => {
  const { data } = await api.get(`/api/runs/${id}`);
  return data;
};

export const deleteRun = async (id: string) => {
  const { data } = await api.delete(`/api/runs/${id}`);
  return data;
};

export const clearAllRuns = async () => {
  const { data } = await api.delete('/api/runs');
  return data;
};

export const clearAllData = async () => {
  // DELETE /api/scripts wipes runs + prompts + scripts + test_cases in FK-safe order
  const { data } = await api.delete('/api/scripts');
  return data;
};

export const cancelRun = async (id: string) => {
  const { data } = await api.patch(`/api/runs/${id}/cancel`);
  return data;
};

export const startRun = async (params: {
  script_id: string;
  environment: string;
  browser: string;
  device: string;
  execution_mode: string;
  browser_version: string;
  tags: string;
  project_id?: string;
}) => {
  const fd = new FormData();
  Object.entries(params).forEach(([k, v]) => { if (v) fd.append(k, v); });
  const { data } = await api.post('/api/run-test', fd);
  return data;
};

// ── Spec Files (from GitHub branch) ──────────────────────────────────────
export const fetchSpecFiles = async (branch?: string, projectId?: string) => {
  const params: Record<string, string> = {};
  if (branch) params.branch = branch;
  if (projectId) params.project_id = projectId;
  const { data } = await api.get('/api/spec-files', { params });
  return data;
};

export const runSpec = async (params: {
  spec_file_path: string;
  branch: string;
  environment: string;
  browser: string;
  device: string;
  execution_mode: string;
  run_target: string;
  tags: string;
  project_id?: string;
}) => {
  console.log('[client.ts] runSpec called with execution_mode:', params.execution_mode);
  const fd = new FormData();
  Object.entries(params).forEach(([k, v]) => { if (v) fd.append(k, v); });
  const { data } = await api.post('/api/run-spec', fd);
  return data;
};

export const ensureBranch = async () => {
  const { data } = await api.post('/api/ensure-branch');
  return data;
};

// ── Framework Cache ───────────────────────────────────────────────────────────
export const refreshFramework = async () => {
  const { data } = await api.post('/api/framework/refresh');
  return data;
};

// ── LLM Provider info ─────────────────────────────────────────────────────────
export const fetchLLMProvider = async () => {
  const { data } = await api.get('/api/llm-provider');
  return data;
};

// ── DOM Crawler ──────────────────────────────────────────────────────────────
export const crawlPage = async (url: string) => {
  const fd = new FormData();
  fd.append('url', url);
  const { data } = await api.post('/api/crawl-page', fd);
  return data as {
    url: string; title: string; screenshot_b64: string;
    element_count: number;
    elements_preview: Array<{ tag: string; selector: string; text: string }>;
  };
};

// ── Script generation (SSE) ───────────────────────────────────────────────────
export function createScriptStream(
  testCaseId: string,
  userInstruction: string,
  onChunk: (text: string) => void,
  onDone: (scriptId: string, isValid: boolean, errors: string) => void,
  onError: (msg: string) => void,
  llmProvider: string = '',   // "anthropic" | "gemini" | "" (use server default)
  projectId: string = '',
  pageUrl: string = '',       // optional: URL for DOM context crawling
): () => void {
  const fd = new FormData();
  fd.append('test_case_id', testCaseId);
  fd.append('user_instruction', userInstruction);
  fd.append('llm_provider', llmProvider);
  if (projectId) fd.append('project_id', projectId);
  if (pageUrl) fd.append('page_url', pageUrl);

  let aborted = false;
  const controller = new AbortController();

  fetch(`${BASE_URL}/api/generate-script`, {
    method: 'POST',
    body: fd,
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done || aborted) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const payload = JSON.parse(line.slice(6));
            if (payload.type === 'chunk') onChunk(payload.text);
            else if (payload.type === 'done')
              onDone(payload.script_id, payload.valid, payload.errors ?? '');
            else if (payload.type === 'error') onError(payload.message);
          } catch {
            /* ignore parse errors */
          }
        }
      }
    })
    .catch((e) => {
      if (!aborted) onError(String(e));
    });

  return () => {
    aborted = true;
    controller.abort();
  };
}

// ── Fix failed script (SSE) ──────────────────────────────────────────────────
export function createFixStream(
  runId: string,
  onChunk: (text: string) => void,
  onDone: (scriptId: string, isValid: boolean, errors: string, filePath: string) => void,
  onError: (msg: string) => void,
  llmProvider: string = '',
  projectId: string = '',
): () => void {
  const fd = new FormData();
  fd.append('run_id', runId);
  fd.append('llm_provider', llmProvider);
  if (projectId) fd.append('project_id', projectId);

  let aborted = false;
  const controller = new AbortController();

  fetch(`${BASE_URL}/api/fix-script`, {
    method: 'POST',
    body: fd,
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done || aborted) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const payload = JSON.parse(line.slice(6));
            if (payload.type === 'chunk') onChunk(payload.text);
            else if (payload.type === 'done')
              onDone(payload.script_id, payload.valid, payload.errors ?? '', payload.file_path ?? '');
            else if (payload.type === 'error') onError(payload.message);
          } catch { /* ignore parse errors */ }
        }
      }
    })
    .catch((e) => {
      if (!aborted) onError(String(e));
    });

  return () => {
    aborted = true;
    controller.abort();
  };
}

// ── Run logs (HTTP fallback — reads Redis history) ────────────────────────────
export const fetchRunLogs = async (runId: string): Promise<string[]> => {
  const { data } = await api.get(`/api/runs/${runId}/logs`);
  return data.lines as string[];
};

// ── WebSocket — live logs ─────────────────────────────────────────────────────
export function connectRunSocket(
  runId: string,
  onLine: (line: string) => void,
  onClose: () => void,
): WebSocket {
  // Use relative WS URL so Vite proxy forwards to ws://localhost:8000
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${window.location.host}/ws/run/${runId}`);
  ws.onmessage = (e) => onLine(e.data);
  ws.onclose = onClose;
  return ws;
}
