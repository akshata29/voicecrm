/** Mock reference data: templates, taxonomy, policies, CRM seed, service modes. All synthetic. */
import type {
  CrmRecordRef,
  PoliciesResponse,
  ResolvedMode,
  SummaryTemplate,
  Taxonomy,
  TemplateInfo,
  Topic,
} from '../types';

// ---- templates (mirror data/templates/*.yaml) ------------------------------------------------------
export const TEMPLATES: SummaryTemplate[] = [
  {
    template_id: 'pb_institutional',
    name: 'Professional Buyer - Institutional call note',
    description:
      'Concise institutional-investor call note for the Professional Buyer group. Structured so the downstream analytics team can mine it: every section maps to a CRM field.',
    business_lines: ['professional_buyer', 'international'],
    call_types: ['discovery', 'product_recommendation', 'follow_up', 'service'],
    language_out: 'en',
    include_relationship_context: false,
    tone: 'concise, factual, third person; no speculation; no personal details',
    max_words: 220,
    sections: [
      { key: 'overview', title: 'Overview', kind: 'text', instruction: '2-3 sentences - who was on the call, the purpose, and the outcome.', max_items: null, required: true, crm_field: 'vcrm_overview' },
      { key: 'client_context', title: 'Client objectives and context', kind: 'list', instruction: 'Mandate, objectives, constraints, timeline and decision process the client stated.', max_items: 5, required: true, crm_field: 'vcrm_clientcontext' },
      { key: 'products_strategies', title: 'Products and strategies discussed', kind: 'list', instruction: 'One line per resolved product (exact fund and share class) plus strategy themes.', max_items: 6, required: true, crm_field: 'vcrm_productsdiscussed' },
      { key: 'concerns', title: 'Concerns and objections', kind: 'list', instruction: 'Questions, concerns, objections or competitor mentions raised by the client.', max_items: 4, required: false, crm_field: 'vcrm_concerns' },
      { key: 'decisions_next_steps', title: 'Decisions and next steps', kind: 'list', instruction: 'Decisions made and agreed next steps with owner and date; must mirror the CRM tasks.', max_items: 6, required: true, crm_field: 'vcrm_nextsteps' },
    ],
  },
  {
    template_id: 'retail_advisor',
    name: 'Retail - Advisor relationship call note',
    description:
      'Relationship-oriented note for retail advisor calls. Keeps non-sensitive rapport facts (interests, preferences) that help the next person covering the advisor; never health, financial-hardship or other sensitive disclosures.',
    business_lines: ['retail'],
    call_types: ['relationship', 'service', 'discovery', 'product_recommendation', 'follow_up'],
    language_out: 'en',
    include_relationship_context: true,
    tone: 'warm but factual, third person; no speculation',
    max_words: 240,
    sections: [
      { key: 'overview', title: 'Overview', kind: 'text', instruction: '2-3 sentences - who was on the call, the purpose, and the outcome.', max_items: null, required: true, crm_field: 'vcrm_overview' },
      { key: 'advisor_situation', title: 'Advisor and client situation', kind: 'list', instruction: 'Business-relevant situation: practice focus, client segments, upcoming events or deadlines.', max_items: 4, required: true, crm_field: 'vcrm_clientcontext' },
      { key: 'products_discussed', title: 'Products discussed', kind: 'list', instruction: 'One line per resolved product (exact fund and share class) with the advisor\u2019s stance.', max_items: 6, required: true, crm_field: 'vcrm_productsdiscussed' },
      { key: 'questions_raised', title: 'Questions and concerns', kind: 'list', instruction: 'Questions or concerns the advisor raised that need an answer.', max_items: 4, required: false, crm_field: 'vcrm_concerns' },
      { key: 'relationship_notes', title: 'Relationship notes', kind: 'list', instruction: 'Non-sensitive rapport facts worth remembering (sports teams, hobbies, preferred contact style).', max_items: 3, required: false, crm_field: 'vcrm_relationshipnotes' },
      { key: 'follow_ups', title: 'Follow-ups', kind: 'list', instruction: 'Agreed follow-ups with owner and date; must mirror the CRM tasks.', max_items: 6, required: true, crm_field: 'vcrm_nextsteps' },
    ],
  },
];

export const TEMPLATE_INFOS: TemplateInfo[] = TEMPLATES.map((template) => ({
  template_id: template.template_id,
  name: template.name,
  description: template.description,
  business_lines: template.business_lines,
  call_types: template.call_types,
  sections: template.sections.map((section) => section.title),
}));

// ---- taxonomy --------------------------------------------------------------------------------------
function topic(topic_id: string, label: string, es: string, synonyms: string[], extra: Partial<Topic> = {}): Topic {
  return {
    topic_id,
    label,
    description: null,
    labels_i18n: { es },
    synonyms,
    synonyms_i18n: {},
    parent_id: null,
    business_lines: [],
    ...extra,
  };
}

export const TAXONOMY: Taxonomy = {
  version: 'mock-2026.11',
  synthetic: true,
  topics: [
    topic('fees_expenses', 'Fees & expenses', 'Comisiones y gastos', ['expense ratio', 'fees', 'load']),
    topic('performance_returns', 'Performance & returns', 'Rentabilidad y rendimiento', ['returns', 'track record']),
    topic('risk_volatility', 'Risk & volatility', 'Riesgo y volatilidad', ['drawdown', 'downside']),
    topic('asset_allocation', 'Asset allocation', 'Asignación de activos', ['allocation', 'rebalancing']),
    topic('equity_strategy', 'Equity strategy', 'Estrategia de renta variable', ['large cap', 'stock selection']),
    topic('fixed_income_duration', 'Fixed income & duration', 'Renta fija y duración', ['duration', 'credit spreads']),
    topic('emerging_markets', 'Emerging markets', 'Mercados emergentes', ['EM', 'frontier']),
    topic('esg_sustainability', 'ESG & sustainability', 'ASG y sostenibilidad', ['ESG', 'screening']),
    topic('share_class_selection', 'Share class selection', 'Selección de clase de acciones', ['class A', 'class C', 'advisor class']),
    topic('liquidity_redemptions', 'Liquidity & redemptions', 'Liquidez y reembolsos', ['redemption', 'liquidity terms']),
    topic('benchmarks_comparison', 'Benchmarks & peer comparison', 'Índices de referencia y comparación', ['benchmark', 'peer group']),
    topic('tax_efficiency', 'Tax efficiency', 'Eficiencia fiscal', ['tax-managed', 'municipal']),
    topic('manager_research', 'Manager & process research', 'Gestores y proceso de inversión', ['portfolio manager', 'process']),
    topic('market_outlook', 'Market outlook', 'Perspectivas de mercado', ['outlook', 'macro']),
    topic('regulatory_compliance', 'Regulatory & compliance', 'Regulación y cumplimiento', ['compliance', 'mandate restrictions']),
    topic('alternatives_hedging', 'Alternatives & hedging', 'Alternativos y coberturas', ['hedge', 'absolute return']),
    topic('platform_access', 'Platform & distribution access', 'Plataformas y distribución', ['platform', 'model portfolio']),
    topic('onboarding_service', 'Onboarding & service', 'Alta y servicio al cliente', ['statement', 'account setup']),
    topic('retirement_planning', 'Retirement planning', 'Planificación de la jubilación', ['retirement', 'income in retirement'], { business_lines: ['retail'] }),
  ],
};

// ---- policies (mirror data/policies/policies.yaml) -------------------------------------------------
export function defaultPolicies(): PoliciesResponse {
  return {
    markets: [
      {
        market: 'US',
        display_name: 'United States',
        transcript_retention: 'off',
        transcript_retention_days: null,
        store_redacted_only: true,
        audio_retention_days: 0,
        summary_retention_days: null,
        notes: 'Example - summary-only market.',
      },
      {
        market: 'UK',
        display_name: 'United Kingdom',
        transcript_retention: 'on',
        transcript_retention_days: 365,
        store_redacted_only: true,
        audio_retention_days: 0,
        summary_retention_days: null,
        notes: 'Example - redacted transcript retained for one year in the vault (never in Dataverse).',
      },
      {
        market: 'EU',
        display_name: 'European Union',
        transcript_retention: 'on',
        transcript_retention_days: 1825,
        store_redacted_only: true,
        audio_retention_days: 0,
        summary_retention_days: null,
        notes: 'Example - redacted transcript retained for five years in the vault (never in Dataverse).',
      },
    ],
    notes: {
      omit_categories: ['payment_card', 'bank_account', 'government_id', 'credentials', 'medical', 'family_health', 'personal_contact'],
      keep_relationship_context: true,
      drop_small_talk: true,
    },
    processing: {
      engine: 'hybrid',
      auto_approve_enabled: false,
      auto_approve_min_confidence: 0.9,
      auto_approve_requires_crm_match: true,
      product_review_threshold: 0.75,
      top_k_products: 3,
      transcription_source_preference: {
        teams: ['platform', 'azure_speech'],
        zoom: ['azure_speech', 'platform'],
        genesys: ['platform', 'azure_speech'],
        phone: ['azure_speech'],
        mobile: ['azure_speech'],
        upload: ['platform', 'azure_speech'],
      },
      default_template_by_business_line: {
        professional_buyer: 'pb_institutional',
        international: 'pb_institutional',
        retail: 'retail_advisor',
      },
    },
  };
}

// ---- service modes: a deliberate mix of live and mock ----------------------------------------------
export const MODES: Record<string, ResolvedMode> = {
  transcription: { service: 'transcription', requested: 'azure_speech', effective: 'azure_speech', live: true, reason: null },
  llm: { service: 'llm', requested: 'foundry', effective: 'hybrid', live: true, reason: null },
  search: {
    service: 'search',
    requested: 'azure_search',
    effective: 'local',
    live: false,
    reason: 'missing settings: VOICECRM_SEARCH_ENDPOINT',
  },
  pii: { service: 'pii', requested: 'local', effective: 'local', live: false, reason: null },
  crm: { service: 'crm', requested: 'local', effective: 'local', live: false, reason: null },
  vault: { service: 'vault', requested: 'local', effective: 'local', live: false, reason: null },
};

// ---- CRM seed (synthetic accounts, contacts, opportunities, users) ---------------------------------
export interface SeedAccount {
  id: string;
  name: string;
  domain: string;
  segment: string;
}
export interface SeedContact {
  id: string;
  name: string;
  email: string;
  phone: string;
  account_id: string;
  title: string;
}
export interface SeedOpportunity {
  id: string;
  name: string;
  account_id: string;
  contact_id: string | null;
  stage: string;
}
export interface SeedUser {
  id: string;
  name: string;
  email: string;
}

export const SEED_USERS: SeedUser[] = [
  { id: 'usr-avery', name: 'Avery Collins', email: 'avery.collins@contoso.example' },
  { id: 'usr-jordan', name: 'Jordan Reyes', email: 'jordan.reyes@contoso.example' },
  { id: 'usr-morgan', name: 'Morgan Patel', email: 'morgan.patel@contoso.example' },
];

export const SEED_ACCOUNTS: SeedAccount[] = [
  { id: 'acc-cedar-ridge', name: 'Cedar Ridge Pension Trust', domain: 'cedarridge.example', segment: 'professional_buyer' },
  { id: 'acc-harborview', name: 'Harborview Endowment Fund', domain: 'harborview.example', segment: 'professional_buyer' },
  { id: 'acc-lakeshore', name: "Lakeshore Teachers' Retirement System", domain: 'lakeshore-trs.example', segment: 'professional_buyer' },
  { id: 'acc-alameda', name: 'Fondo de Pensiones Alameda', domain: 'alameda-fp.example', segment: 'professional_buyer' },
  { id: 'acc-evergreen', name: 'Evergreen Advisory Group', domain: 'evergreenadvisory.example', segment: 'retail' },
  { id: 'acc-summit', name: 'Summit Peak Wealth Partners', domain: 'summitpeak.example', segment: 'retail' },
  { id: 'acc-highgate', name: 'Highgate Pension Scheme', domain: 'highgate-ps.example', segment: 'international' },
  { id: 'acc-nordlys', name: 'Nordlys Pension Fund', domain: 'nordlys.example', segment: 'international' },
];

export const SEED_CONTACTS: SeedContact[] = [
  { id: 'con-marcus', name: 'Marcus Whitfield', email: 'marcus.whitfield@cedarridge.example', phone: '+1 555 0101', account_id: 'acc-cedar-ridge', title: 'Chief Investment Officer' },
  { id: 'con-priya', name: 'Priya Nair', email: 'priya.nair@cedarridge.example', phone: '+1 555 0102', account_id: 'acc-cedar-ridge', title: 'Investment Analyst' },
  { id: 'con-helen', name: 'Helen Okafor', email: 'helen.okafor@harborview.example', phone: '+1 555 0111', account_id: 'acc-harborview', title: 'Chief Investment Officer' },
  { id: 'con-daniel', name: 'Daniel Brooks', email: 'daniel.brooks@lakeshore-trs.example', phone: '+1 555 0121', account_id: 'acc-lakeshore', title: 'Head of Fixed Income' },
  { id: 'con-sofia', name: 'Sofía Marín', email: 'sofia.marin@alameda-fp.example', phone: '+1 555 0131', account_id: 'acc-alameda', title: 'Directora de Inversiones' },
  { id: 'con-sofia-v', name: 'Sofía Marín Vega', email: 'sofia.marinvega@alameda-fp.example', phone: '+1 555 0132', account_id: 'acc-alameda', title: 'Analista de Riesgos' },
  { id: 'con-jordan-e', name: 'Jordan Ellis', email: 'jordan.ellis@evergreenadvisory.example', phone: '+1 555 0141', account_id: 'acc-evergreen', title: 'Senior Advisor' },
  { id: 'con-amelia', name: 'Amelia Torres', email: 'amelia.torres@summitpeak.example', phone: '+1 555 0151', account_id: 'acc-summit', title: 'Financial Advisor' },
  { id: 'con-oliver', name: 'Oliver Hartley', email: 'oliver.hartley@highgate-ps.example', phone: '+44 20 5550 0161', account_id: 'acc-highgate', title: 'Head of Investments' },
  { id: 'con-freja', name: 'Freja Lindqvist', email: 'freja.lindqvist@nordlys.example', phone: '+45 5550 0171', account_id: 'acc-nordlys', title: 'Portfolio Manager' },
];

export const SEED_OPPORTUNITIES: SeedOpportunity[] = [
  { id: 'opp-cedar-equity', name: 'Cedar Ridge - Core equity mandate', account_id: 'acc-cedar-ridge', contact_id: 'con-marcus', stage: 'Propose' },
  { id: 'opp-harbor-income', name: 'Harborview - Growth allocation review', account_id: 'acc-harborview', contact_id: 'con-helen', stage: 'Qualify' },
  { id: 'opp-lakeshore-bond', name: 'Lakeshore - Short-duration bond sleeve', account_id: 'acc-lakeshore', contact_id: 'con-daniel', stage: 'Develop' },
  { id: 'opp-alameda-global', name: 'Alameda - Renta variable global', account_id: 'acc-alameda', contact_id: 'con-sofia', stage: 'Qualify' },
  { id: 'opp-evergreen-models', name: 'Evergreen - Model portfolio refresh', account_id: 'acc-evergreen', contact_id: 'con-jordan-e', stage: 'Develop' },
  { id: 'opp-highgate-multi', name: 'Highgate - Multi-asset allocation', account_id: 'acc-highgate', contact_id: 'con-oliver', stage: 'Qualify' },
];

export function accountRef(accountId: string): CrmRecordRef {
  const account = SEED_ACCOUNTS.find((item) => item.id === accountId);
  return { entity: 'account', id: accountId, name: account?.name ?? accountId };
}

export function contactRef(contactId: string): CrmRecordRef {
  const contact = SEED_CONTACTS.find((item) => item.id === contactId);
  return { entity: 'contact', id: contactId, name: contact?.name ?? contactId };
}

export function opportunityRef(opportunityId: string): CrmRecordRef {
  const opportunity = SEED_OPPORTUNITIES.find((item) => item.id === opportunityId);
  return { entity: 'opportunity', id: opportunityId, name: opportunity?.name ?? opportunityId };
}

// ---- illustrative price table (mirror data/pricing.yaml: Azure list prices, East US 2, 2026-11-03) ------------------
export const PRICING = {
  speechPerAudioHour: 0.36,
  llmInputPer1M: 2.0,
  llmOutputPer1M: 8.0,
  languagePiiPer1kRecords: 0.75,
};
