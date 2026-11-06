import { BoltIcon } from '@heroicons/react/24/outline';
import { usePreflight } from '@/api/hooks';
import type { PreflightReadiness } from '@/api/types';
import { AudioScenario } from '@/components/live/AudioScenario';
import { BulkScenario } from '@/components/live/BulkScenario';
import { ServiceReadiness } from '@/components/live/ServiceReadiness';
import { TeamsScenario } from '@/components/live/TeamsScenario';
import { PageHeader } from '@/components/ui/Field';
import { useDocumentTitle } from '@/lib/hooks';

const JUMPS: Array<{ letter: string; label: string; href: string }> = [
  { letter: 'A', label: 'Audio', href: '#scenario-a' },
  { letter: 'B', label: 'Teams', href: '#scenario-b' },
  { letter: 'C', label: 'Bulk', href: '#scenario-c' },
];

/** Verdicts are "A  audio -> ...", "B  Teams call -> ...", "C  bulk run ...": the first letter names the scenario. */
const verdictFor = (items: PreflightReadiness[] | undefined, letter: string) => items?.find((item) => item.scenario.startsWith(letter));

/** Runs the three demo scenarios against the real services from the browser: no command line needed. */
export default function LiveDemoPage() {
  useDocumentTitle('Live demo');
  const check = usePreflight();
  const readiness = check.data?.readiness;

  return (
    <>
      <PageHeader
        title="Live demo"
        icon={BoltIcon}
        description="Run the real thing from here: play a recorded call through Azure Speech, Language and a model; take in a Teams meeting; or send hundreds of calls at once. Every figure comes from the running pipeline."
        actions={
          <nav aria-label="Scenarios" className="flex items-center gap-1.5">
            {JUMPS.map((jump) => (
              <a key={jump.letter} href={jump.href} className="btn btn-secondary btn-sm">
                <span className="font-bold text-brand-600">{jump.letter}</span> {jump.label}
              </a>
            ))}
          </nav>
        }
      />
      <div className="space-y-10">
        <ServiceReadiness check={check} />
        <AudioScenario verdict={verdictFor(readiness, 'A')} />
        <TeamsScenario verdict={verdictFor(readiness, 'B')} />
        <BulkScenario verdict={verdictFor(readiness, 'C')} />
      </div>
    </>
  );
}
