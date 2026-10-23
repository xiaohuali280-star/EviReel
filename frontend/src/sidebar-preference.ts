export const SIDEBAR_PREFERENCE_KEY = 'evireel:sidebar-collapsed';

export function sidebarCollapsed(raw: string | null) {
  return raw === 'true';
}
