import { lazy, Suspense } from 'react';
import { Link, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { EmptyState, LoadingBlock } from '@/components/ui/StateViews';
import ArchitecturePage from '@/pages/ArchitecturePage';
import CapturePage from '@/pages/CapturePage';
import CataloguePage from '@/pages/CataloguePage';
import CrmPage from '@/pages/CrmPage';
import InboxPage from '@/pages/InboxPage';
import LiveDemoPage from '@/pages/LiveDemoPage';
import PoliciesPage from '@/pages/PoliciesPage';
import ReviewPage from '@/pages/ReviewPage';
import SourcesPage from '@/pages/SourcesPage';

// Recharts is the heaviest dependency; only the Scoreboard needs it.
const ScoreboardPage = lazy(() => import('@/pages/ScoreboardPage'));

function NotFound() {
  return (
    <div className="card">
      <EmptyState
        title="Page not found"
        description="That address does not exist in VoiceCRM."
        action={
          <Link to="/" className="btn btn-primary">
            Back to the inbox
          </Link>
        }
      />
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<InboxPage />} />
        <Route path="conversations/:id" element={<ReviewPage />} />
        <Route path="live" element={<LiveDemoPage />} />
        <Route path="sources" element={<SourcesPage />} />
        <Route path="capture" element={<CapturePage />} />
        <Route path="policies" element={<PoliciesPage />} />
        <Route path="catalogue" element={<CataloguePage />} />
        <Route path="crm" element={<CrmPage />} />
        <Route
          path="scoreboard"
          element={
            <Suspense fallback={<LoadingBlock label="Loading the scoreboard…" />}>
              <ScoreboardPage />
            </Suspense>
          }
        />
        <Route path="architecture" element={<ArchitecturePage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
