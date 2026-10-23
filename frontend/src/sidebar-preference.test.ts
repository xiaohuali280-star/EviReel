import {expect, it} from 'vitest';
import {sidebarCollapsed} from './sidebar-preference';

it('only a valid explicit collapsed preference hides the desktop library', () => {
  expect(sidebarCollapsed('true')).toBe(true);
  for (const raw of [null, '', 'false', '1', 'TRUE', '{}', 'damaged']) expect(sidebarCollapsed(raw)).toBe(false);
});
