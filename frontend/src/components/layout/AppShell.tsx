import { Bars3Icon, XMarkIcon } from '@heroicons/react/24/outline';
import { useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { EnvChips, SyntheticBadge } from './EnvChips';
import { BrandMark, SideNavContent } from './SideNav';

export function AppShell() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { pathname } = useLocation();

  useEffect(() => {
    setDrawerOpen(false);
    // The scroll container is <main>, which survives route changes: start every page at the top.
    document.getElementById('main')?.scrollTo({ top: 0 });
  }, [pathname]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawerOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [drawerOpen]);

  return (
    <div className="flex h-dvh overflow-hidden bg-canvas">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-[70] focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-semibold focus:text-brand-700 focus:shadow-pop">
        Skip to content
      </a>

      <aside className="hidden w-60 shrink-0 border-r border-neutral-200 bg-white md:block">
        <SideNavContent />
      </aside>

      {drawerOpen ? (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button type="button" aria-label="Close navigation" className="absolute inset-0 bg-neutral-900/40" onClick={() => setDrawerOpen(false)} />
          <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] animate-slide-in-left bg-white shadow-pop">
            <button type="button" aria-label="Close navigation" onClick={() => setDrawerOpen(false)} className="absolute right-2 top-3 rounded-md p-2 text-neutral-500 hover:bg-neutral-100">
              <XMarkIcon aria-hidden="true" className="h-5 w-5" />
            </button>
            <SideNavContent onNavigate={() => setDrawerOpen(false)} />
          </div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="z-30 shrink-0 border-b border-neutral-200 bg-white">
          <div className="flex h-14 items-center gap-3 px-3 sm:px-4 md:px-6">
            <button type="button" onClick={() => setDrawerOpen(true)} className="-ml-1 rounded-md p-2 text-neutral-600 hover:bg-neutral-100 md:hidden" aria-label="Open navigation" aria-expanded={drawerOpen}>
              <Bars3Icon aria-hidden="true" className="h-6 w-6" />
            </button>
            <Link to="/" className="flex items-center gap-2 md:hidden">
              <BrandMark className="h-8 w-8" />
              <span className="text-base font-semibold text-neutral-900">VoiceCRM</span>
            </Link>
            <EnvChips className="no-scrollbar hidden min-w-0 flex-1 overflow-x-auto py-1 md:flex" />
            <div className="ml-auto flex items-center gap-2 md:ml-0">
              <SyntheticBadge />
            </div>
          </div>
          <EnvChips className="no-scrollbar overflow-x-auto px-3 pb-2 sm:px-4 md:hidden" />
        </header>

        {/* relative: absolutely positioned children (sr-only inputs and labels) stay inside this scroller. Without it they are placed against the page itself and stretch it, so the window scrolls behind the app shell. */}
        <main id="main" tabIndex={-1} className="relative min-h-0 flex-1 overflow-y-auto focus:outline-none">
          <div className="mx-auto w-full max-w-[1400px] px-3 py-4 sm:px-4 md:px-6 md:py-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
