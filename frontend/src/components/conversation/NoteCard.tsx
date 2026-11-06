import { CheckIcon, DocumentTextIcon, PencilSquareIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { useState } from 'react';
import type { NoteSection } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import type { Tone } from '@/components/ui/types';
import { humanize } from '@/lib/format';

const LANGUAGE_NAMES: Record<string, string> = { es: 'Spanish', en: 'English', fr: 'French', de: 'German', pt: 'Portuguese' };

/** Extraction flags are machine keys (some carry a value after a colon); this turns them into short, honest labels. */
function flagInfo(flag: string): { label: string; tone: Tone; hint?: string } {
  const [key = flag, value] = flag.split(':', 2);
  switch (key) {
    case 'pricing_discussed':
      return { label: 'Pricing discussed', tone: 'info', hint: 'Fees or prices came up on the call' };
    case 'competitor_named':
      return { label: 'Competitor named', tone: 'info', hint: 'A competing product or firm was mentioned' };
    case 'source_language':
      return { label: `Source language: ${LANGUAGE_NAMES[value ?? ''] ?? value ?? 'other'}`, tone: 'info', hint: 'The CRM note is written in the template language' };
    case 'free_text_needs_llm_engine':
      return { label: 'Structured note', tone: 'neutral', hint: 'The rules engine builds this note from structured elements; the LLM engine writes richer free text' };
    case 'llm_fallback':
      return { label: 'LLM failed: used rules', tone: 'warning', hint: 'The language model call failed, so the rules engine produced this proposal' };
    case 'engine_unavailable':
      return { label: `Engine "${value ?? 'llm'}" not available: used rules`, tone: 'warning', hint: 'The requested engine is not configured (mock or local mode)' };
    default:
      return { label: humanize(flag.replace(':', ' ')), tone: 'neutral' };
  }
}

interface NoteCardProps {
  sections: NoteSection[];
  markdown: string;
  templateName: string;
  /** Extraction flags from the engine (see flagInfo). */
  flags?: string[];
  edits: Record<string, string>;
  editable: boolean;
  /** Sections the backend rebuilds from the products / tasks below, mapped to the reason they cannot be edited here. */
  locked?: Record<string, string>;
  /** Pass null to clear an edit. */
  onEdit: (key: string, text: string | null) => void;
}

const baseline = (section: NoteSection): string => section.text ?? section.items.join('\n');

/** The note exactly as it would land in the CRM, section by section (sections come from the selected template). */
export function NoteCard({ sections, markdown, templateName, flags = [], edits, editable, locked = {}, onEdit }: NoteCardProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  const startEdit = (section: NoteSection) => {
    setEditing(section.key);
    setDraft(edits[section.key] ?? baseline(section));
  };

  const save = (section: NoteSection) => {
    const next = draft.trim();
    onEdit(section.key, next === baseline(section) || next === '' ? null : next);
    setEditing(null);
  };

  return (
    <Card aria-labelledby="note-title">
      <CardHeader
        id="note-title"
        title="Note for the CRM"
        subtitle={`Rendered with the “${templateName}” template`}
        icon={DocumentTextIcon}
        actions={<Badge tone="brand">{sections.length} sections</Badge>}
      />
      <CardBody className="space-y-4">
        {flags.length > 0 ? (
          <ul className="flex flex-wrap gap-1.5" aria-label="Extraction flags">
            {flags.map((flag) => {
              const info = flagInfo(flag);
              return (
                <li key={flag}>
                  <Badge tone={info.tone} title={info.hint}>
                    {info.label}
                  </Badge>
                </li>
              );
            })}
          </ul>
        ) : null}
        {sections.map((section) => {
          const isEditing = editing === section.key;
          const edited = edits[section.key];
          const lockedReason = locked[section.key];
          return (
            <section key={section.key} aria-labelledby={`section-${section.key}`}>
              <div className="flex items-center justify-between gap-2">
                <h3 id={`section-${section.key}`} className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
                  {section.title}
                  {edited !== undefined ? <span className="ml-2 rounded bg-amber-100 px-1.5 py-px text-[10px] font-semibold normal-case tracking-normal text-amber-900">edited</span> : null}
                  {lockedReason ? <span className="ml-2 text-[10px] font-medium normal-case tracking-normal text-neutral-400">{lockedReason}</span> : null}
                </h3>
                {editable && !lockedReason && !isEditing ? (
                  <button
                    type="button"
                    onClick={() => startEdit(section)}
                    className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium text-neutral-500 hover:bg-neutral-100 hover:text-neutral-800"
                    aria-label={`Edit ${section.title}`}
                  >
                    <PencilSquareIcon aria-hidden="true" className="h-3.5 w-3.5" /> Edit
                  </button>
                ) : null}
              </div>
              {isEditing ? (
                <div className="mt-1.5 space-y-2">
                  <label htmlFor={`edit-${section.key}`} className="sr-only">
                    {section.title}
                  </label>
                  <textarea
                    id={`edit-${section.key}`}
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    rows={section.text !== null ? 4 : Math.min(10, Math.max(4, section.items.length + 1))}
                    className="input"
                  />
                  {section.text === null ? <p className="text-xs text-neutral-500">One bullet per line.</p> : null}
                  <div className="flex gap-2">
                    <Button size="sm" variant="primary" icon={CheckIcon} onClick={() => save(section)}>
                      Save
                    </Button>
                    <Button size="sm" variant="ghost" icon={XMarkIcon} onClick={() => setEditing(null)}>
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : section.text !== null ? (
                <p className="mt-1 text-sm leading-6 text-neutral-800">{edited ?? section.text}</p>
              ) : (
                <SectionItems items={edited !== undefined ? edited.split('\n').map((line) => line.replace(/^[-*\u2022]\s*/, '').trim()).filter(Boolean) : section.items} />
              )}
            </section>
          );
        })}
        <details className="rounded-md border border-neutral-200 bg-neutral-50 text-xs">
          <summary className="cursor-pointer select-none px-3 py-2 font-medium text-neutral-600 hover:text-neutral-900">Raw note (markdown, as sent to the CRM)</summary>
          <pre className="overflow-x-auto whitespace-pre-wrap border-t border-neutral-200 px-3 py-2 text-[11px] leading-5 text-neutral-700">{markdown}</pre>
        </details>
      </CardBody>
    </Card>
  );
}

function SectionItems({ items }: { items: string[] }) {
  if (items.length === 0) return <p className="mt-1 text-sm italic text-neutral-400">Nothing recorded for this section.</p>;
  return (
    <ul className="mt-1 list-disc space-y-1 pl-5 text-sm leading-6 text-neutral-800 marker:text-neutral-400">
      {items.map((item, index) => (
        <li key={`${index}-${item}`}>{item}</li>
      ))}
    </ul>
  );
}
