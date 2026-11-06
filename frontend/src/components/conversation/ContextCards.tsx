import { BuildingOffice2Icon, ExclamationTriangleIcon, HeartIcon, LinkIcon, UserCircleIcon, BriefcaseIcon } from '@heroicons/react/24/outline';
import type { CrmMatch, CrmRecordRef, RelationshipFact } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import type { IconType } from '@/components/ui/types';
import { formatPercent, humanize } from '@/lib/format';
import { EvidenceQuotes } from './TranscriptPolicy';

export function RelationshipCard({ facts }: { facts: RelationshipFact[] }) {
  return (
    <Card aria-labelledby="relationship-title">
      <CardHeader id="relationship-title" title="Relationship facts" subtitle="Non-sensitive rapport worth remembering (retail template)" icon={HeartIcon} actions={<Badge tone="success">{facts.length} kept</Badge>} />
      <CardBody>
        {facts.length === 0 ? (
          <p className="text-sm text-neutral-500">No relationship facts were kept for this call.</p>
        ) : (
          <ul className="space-y-2">
            {facts.map((fact, index) => (
              <li key={`${index}-${fact.text}`} className="rounded-lg border border-emerald-200 bg-emerald-50/60 px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <HeartIcon aria-hidden="true" className="h-4 w-4 shrink-0 text-emerald-600" />
                  <p className="text-sm font-medium text-neutral-900">{fact.text}</p>
                  <Badge tone="success">{humanize(fact.category)}</Badge>
                </div>
                <EvidenceQuotes evidence={fact.evidence} className="mt-1 pl-6" />
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

function MatchRow({ icon: Icon, label, record }: { icon: IconType; label: string; record: CrmRecordRef | null }) {
  return (
    <div className="flex items-center gap-3 py-2">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-neutral-100 text-neutral-500">
        <Icon aria-hidden="true" className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <p className="text-xs font-semibold uppercase tracking-wide text-neutral-400">{label}</p>
        {record ? (
          <p className="truncate text-sm font-medium text-neutral-900">
            {record.name ?? record.id} <span className="ml-1 font-mono text-xs font-normal text-neutral-400">{record.id}</span>
          </p>
        ) : (
          <p className="text-sm text-neutral-400">None matched</p>
        )}
      </div>
    </div>
  );
}

export function CrmMatchCard({ match }: { match: CrmMatch }) {
  const matched = match.account !== null;
  return (
    <Card aria-labelledby="crm-match-title">
      <CardHeader
        id="crm-match-title"
        title="CRM match"
        subtitle="Where the call, tasks and insight will be attached"
        icon={LinkIcon}
        actions={
          matched ? (
            <Badge tone={match.ambiguous ? 'warning' : 'success'}>
              {humanize(match.method)} · {formatPercent(match.confidence)}
            </Badge>
          ) : (
            <Badge tone="neutral">No match</Badge>
          )
        }
      />
      <CardBody>
        <div className="divide-y divide-neutral-100">
          <MatchRow icon={BuildingOffice2Icon} label="Account" record={match.account} />
          <MatchRow icon={UserCircleIcon} label="Contact" record={match.contact} />
          <MatchRow icon={BriefcaseIcon} label="Opportunity" record={match.opportunity} />
        </div>
        {match.ambiguous ? (
          <div role="alert" className="mt-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <p className="flex items-center gap-2 font-semibold">
              <ExclamationTriangleIcon aria-hidden="true" className="h-4 w-4 shrink-0 text-amber-600" />
              Ambiguous match: more than one record fits
            </p>
            {match.candidates.length > 0 ? (
              <ul className="mt-1 list-inside list-disc text-xs text-amber-800">
                {match.candidates.map((candidate) => (
                  <li key={candidate.id}>
                    {candidate.name ?? candidate.id} <span className="font-mono text-amber-700">({candidate.id})</span>
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="mt-1 text-xs text-amber-800">The call will be logged against the account; check the contact in the CRM afterwards.</p>
          </div>
        ) : null}
        {!matched ? (
          <p className="mt-3 text-sm text-neutral-600">No account matched the participants, so the call, tasks and insight will be logged without a “regarding” record.</p>
        ) : null}
      </CardBody>
    </Card>
  );
}
