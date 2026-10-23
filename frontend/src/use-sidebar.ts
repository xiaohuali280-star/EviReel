import {useLayoutEffect, useRef, useState} from 'react';
import {SIDEBAR_PREFERENCE_KEY, sidebarCollapsed} from './sidebar-preference';

function savedCollapsed() {
  try {return sidebarCollapsed(localStorage.getItem(SIDEBAR_PREFERENCE_KEY))} catch {return false}
}

export function useSidebar() {
  const [collapsed, setCollapsed] = useState(savedCollapsed);
  const [mobile, setMobile] = useState(() => matchMedia('(max-width: 600px)').matches);
  const [mobileExpanded, setMobileExpanded] = useState(false);
  const toggleButton = useRef<HTMLButtonElement>(null);
  const library = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const scroll = useRef(0);
  const expanded = mobile ? mobileExpanded : !collapsed;
  const current = useRef({expanded, collapsed, mobileExpanded});
  current.current = {expanded, collapsed, mobileExpanded};
  const rememberScroll = () => {if (list.current && current.current.expanded) scroll.current = list.current.scrollTop};
  const focusToggle = () => {
    if (library.current?.contains(document.activeElement)) toggleButton.current?.focus({preventScroll: true});
  };
  useLayoutEffect(() => {
    const media = matchMedia('(max-width: 600px)');
    const change = () => {
      rememberScroll();
      const nextExpanded = media.matches ? current.current.mobileExpanded : !current.current.collapsed;
      if (!nextExpanded) focusToggle();
      setMobile(media.matches);
    };
    media.addEventListener('change', change);
    return () => media.removeEventListener('change', change);
  }, []);
  useLayoutEffect(() => {if (expanded && list.current) list.current.scrollTop = scroll.current}, [expanded]);

  const setExpanded = (next: boolean) => {
    rememberScroll(); if (!next) focusToggle();
    if (mobile) setMobileExpanded(next);
    else {
      setCollapsed(!next);
      try {localStorage.setItem(SIDEBAR_PREFERENCE_KEY, String(!next))} catch { /* session state still works */ }
    }
  };
  return {expanded, mobile, toggleButton, library, list, rememberScroll,
    toggle: () => setExpanded(!expanded), expand: () => setExpanded(true),
    selected: () => {if (mobile) setExpanded(false)},
  };
}
