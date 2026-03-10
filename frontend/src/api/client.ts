import axios from 'axios';

// Use relative URLs so requests go through the Vite dev proxy → avoids CORS entirely.
// Vite config: '/api' proxied to http://localhost:8000, '/ws' proxied to ws://localhost:8000
const BASE_URL = '';

export const api = axios.create({ baseURL: BASE_URL });

// ── Excel Upload ──────────────────────────────────────────────────────────────
export const uploadExcel = async (file: File) => {
  const fd = new FormData();
  fd.append('file', file);
  const { data } = await api.post('/api/parse-excel', fd);
  return data;
};

// ── Test Cases ────────────────────────────────────────────────────────────────
export const fetchTestCases = async () => {
  const { data } = await api.get('/api/test-cases');
  return data;
};

// ── Scripts ───────────────────────────────────────────────────────────────────
export const fetchScripts = async () => {
  const { data } = await api.get('/api/scripts');
  return data;
};

export const fetchScript = async (id: string) => {
  const { data } = await api.get(`/api/scripts/${id}`);
  return data;
};

// ── Runs ──────────────────────────────────────────────────────────────────────
export const fetchRuns = async () => {
  const { data } = await api.get('/api/runs');
  return data;
};

export const fetchRun = async (id: string) => {
  const { data } = await api.get(`/api/runs/${id}`);
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
}) => {
  const fd = new FormData();
  Object.entries(params).forEach(([k, v]) => fd.append(k, v));
  const { data } = await api.post('/api/run-test', fd);
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

// ── Script generation (SSE) ───────────────────────────────────────────────────
export function createScriptStream(
  testCaseId: string,
  userInstruction: string,
  onChunk: (text: string) => void,
  onDone: (scriptId: string, isValid: boolean, errors: string) => void,
  onError: (msg: string) => void,
  llmProvider: string = '',   // "anthropic" | "gemini" | "" (use server default)
): () => void {
  const fd = new FormData();
  fd.append('test_case_id', testCaseId);
  fd.append('user_instruction', userInstruction);
  fd.append('llm_provider', llmProvider);

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
