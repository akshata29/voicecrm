import { PlusIcon, TagIcon, XMarkIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useId, useState } from 'react';
import type { TopicTag, Topic } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { formatPercent } from '@/lib/format';

interface TopicsCardProps {
  tags: TopicTag[];
  selected: string[];
  taxonomy: Topic[] | undefined;
  businessLine: string;
  readOnly: boolean;
  onChange: (ids: string[]) => void;
}

/** Topics come from the controlled taxonomy (they map to CRM dropdown fields): add or remove, never free text. */
export function TopicsCard({ tags, selected, taxonomy, businessLine, readOnly, onChange }: TopicsCardProps) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const labelOf = (id: string) => tags.find((tag) => tag.topic_id === id)?.label ?? taxonomy?.find((topic) => topic.topic_id === id)?.label ?? id;
  const confidenceOf = (id: string) => tags.find((tag) => tag.topic_id === id)?.confidence;
  const available = (taxonomy ?? []).filter((topic) => !selected.includes(topic.topic_id) && (topic.business_lines.length === 0 || topic.business_lines.includes(businessLine)));

  return (
    <Card aria-labelledby="topics-title">
      <CardHeader
        id="topics-title"
        title="Topics"
        subtitle="Controlled list, mapped to CRM option fields"
        icon={TagIcon}
        actions={
          !readOnly ? (
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              aria-controls={panelId}
              disabled={!taxonomy}
              className="btn btn-secondary btn-sm"
            >
              <PlusIcon aria-hidden="true" className="h-3.5 w-3.5" /> Add topic
            </button>
          ) : null
        }
      />
      <CardBody className="space-y-3">
        {selected.length === 0 ? (
          <p className="text-sm text-neutral-500">No topics selected. {readOnly ? '' : 'Add one from the list.'}</p>
        ) : (
          <ul className="flex flex-wrap gap-2" aria-label="Selected topics">
            {selected.map((id) => {
              const confidence = confidenceOf(id);
              return (
                <li key={id}>
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-50 py-1 pl-3 pr-1.5 text-sm font-medium text-brand-800 ring-1 ring-inset ring-brand-200">
                    {labelOf(id)}
                    {confidence !== undefined ? (
                      <span className="tabular text-xs font-normal text-brand-600" title={`Model confidence ${formatPercent(confidence)}`}>
                        {formatPercent(confidence)}
                      </span>
                    ) : (
                      <Badge tone="warning" className="!py-0">
                        added
                      </Badge>
                    )}
                    {!readOnly ? (
                      <button
                        type="button"
                        onClick={() => onChange(selected.filter((item) => item !== id))}
                        className="rounded-full p-0.5 text-brand-500 hover:bg-brand-100 hover:text-brand-800"
                        aria-label={`Remove topic ${labelOf(id)}`}
                      >
                        <XMarkIcon aria-hidden="true" className="h-4 w-4" />
                      </button>
                    ) : (
                      <span className="w-1" />
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        {open && !readOnly ? (
          <div id={panelId} className="rounded-lg border border-neutral-200 bg-neutral-50 p-3">
            <p className="mb-2 text-xs font-semibold text-neutral-600">Add from the taxonomy</p>
            {available.length === 0 ? (
              <p className="text-sm text-neutral-500">Every topic is already selected.</p>
            ) : (
              <ul className="flex flex-wrap gap-2">
                {available.map((topic) => (
                  <li key={topic.topic_id}>
                    <button
                      type="button"
                      onClick={() => onChange([...selected, topic.topic_id])}
                      title={topic.description ?? undefined}
                      className={clsx('inline-flex items-center gap-1 rounded-full border border-neutral-300 bg-white px-2.5 py-1 text-sm text-neutral-700 hover:border-brand-400 hover:bg-brand-50 hover:text-brand-800')}
                    >
                      <PlusIcon aria-hidden="true" className="h-3.5 w-3.5" /> {topic.label}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
