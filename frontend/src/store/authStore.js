import { create } from 'zustand';

try {
  localStorage.removeItem('token');
} catch {
  // Storage may be disabled by the browser; authentication does not depend on it.
}

export const useAuthStore = create((set) => ({
  isAuthenticated: false,
  user: null,

  setAuthenticated: (isAuthenticated) => set({ isAuthenticated }),

  logout: () => {
    set({ isAuthenticated: false, user: null });
  },
}));
