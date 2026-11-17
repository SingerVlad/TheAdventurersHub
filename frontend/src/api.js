import axios from 'axios';
export const ipPublica = import.meta.env.VITE_PUBLIC_URL || 'http://127.0.0.1:5000'
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || `${ipPublica}/api`,
  withCredentials: true,
});
export const ipFoundry = import.meta.env.VITE_FOUNDRY_URL || 'http://127.0.0.1:30000'

let csrfToken = null;

export function setCsrfToken(token) {
  csrfToken = token;
}

api.interceptors.request.use((config) => {
  if (csrfToken && !['get', 'head', 'options'].includes(config.method?.toLowerCase())) {
    config.headers.set('X-CSRF-Token', csrfToken);
  }
  return config;
});
