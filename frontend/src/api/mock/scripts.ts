/**
 * Scripted demo calls for the mock API. Everything here is SYNTHETIC: invented people, organisations and funds.
 * Transcript text is stored already redacted (tokens such as [PAYMENT_CARD], empty text where a segment was omitted),
 * mirroring what the real API sends - sensitive text never exists in this file.
 */
import type { Channel, Direction, FixtureInfo, MentionType, OwnerRole, PolicyAction } from '../types';

export type Who = 'seller' | 'customer' | 'internal';

export interface Turn {
  who: Who;
  text: string;
  locale?: string;
}

export interface MentionSpec {
  surface: string;
  fund: string;
  stated: string | null;
  chosen: string | null;
  confidence: number;
  type: MentionType;
  seg: number[];
  candidates: Array<{ cls: string; score: number; matched?: string[]; fund?: string }>;
  reasons?: string[];
}

export interface TopicSpec {
  id: string;
  confidence: number;
  seg: number[];
}

export interface TaskSpec {
  title: string;
  description?: string;
  owner?: OwnerRole;
  dueDays: number | null;
  dueText?: string;
  basis: 'stated' | 'inferred' | 'none';
  confidence: number;
  seg: number[];
}

export interface NoteFacts {
  overview: string;
  context: string[];
  concerns: string[];
  relationship: string[];
}

export interface PersonSpec {
  name: string;
  email: string;
  phone?: string;
  title?: string;
}

export interface MockScript {
  info: FixtureInfo;
  direction: Direction;
  seller: PersonSpec;
  customer: PersonSpec;
  org: string;
  accountId: string;
  contactId: string | null;
  opportunityId: string | null;
  match: { method: string; confidence: number; ambiguous?: boolean; candidateContactIds?: string[] };
  mentions: MentionSpec[];
  topics: TopicSpec[];
  tasks: TaskSpec[];
  facts: NoteFacts;
  policyActions: PolicyAction[];
  turns: Turn[];
  flags?: string[];
  confidence: number;
}

const sellerAvery: PersonSpec = { name: 'Avery Collins', email: 'avery.collins@contoso.example' };
const sellerJordan: PersonSpec = { name: 'Jordan Reyes', email: 'jordan.reyes@contoso.example' };
const sellerMorgan: PersonSpec = { name: 'Morgan Patel', email: 'morgan.patel@contoso.example' };

const S = (text: string, locale?: string): Turn => ({ who: 'seller', text, locale });
const C = (text: string, locale?: string): Turn => ({ who: 'customer', text, locale });

function info(
  fixture_id: string,
  title: string,
  description: string,
  channel: Channel,
  fields: Pick<FixtureInfo, 'language' | 'market' | 'business_line' | 'call_type' | 'tags' | 'duration_minutes'>,
  turns: number,
): FixtureInfo {
  return { fixture_id, title, description, channel, turns, ...fields };
}

// ---- 1. hero: Class C vs Class A -------------------------------------------------------------------
const heroTurns: Turn[] = [
  S('Thanks for joining, Marcus. I wanted to follow up on the large-cap equity sleeve the investment committee is considering.'),
  C("Appreciate the time, Avery. We're looking at adding about five percent to U.S. large-cap equity, and the committee meets on the twentieth."),
  S('Understood. Are you comparing share classes, or do you already have a preference?'),
  C("We'd like the Contoso Equity Fund, class C, for the transition period. I want to be sure it isn't the class A shares, since the load structure is different."),
  S("Right, class C, the level-load shares. I'll confirm that in writing."),
  C("We also hold the Equity Income Fund, Advisor shares, in the existing sleeve, and we're happy with it."),
  C('My main concern is the total ongoing cost of class C compared to the Advisor class over a three year horizon.'),
  S("Fair point. I'll send you a fee schedule and an expense comparison by Friday."),
  C('Great. And can you set up time with our investment committee before the twentieth?'),
  S("Yes, I'll schedule that and send options early next week. Anything on ESG screening?"),
  C('Not for now. Just the fees and the committee session. You can reach me on [PHONE_NUMBER] if email is slow.'),
  S("Perfect, I'll follow up on both. Thanks, Marcus."),
];

const hero: MockScript = {
  info: info(
    'pb-teams-equity-class-c',
    'Class C vs Class A - Cedar Ridge equity sleeve',
    "The hero call: the client says 'class C' and explicitly NOT class A, next to an Advisor-class holding. The exact share class must survive end to end.",
    'teams',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'product_recommendation', tags: ['hero', 'class_trap', 'teams'], duration_minutes: 12 },
    heroTurns.length,
  ),
  direction: 'meeting',
  seller: sellerAvery,
  customer: { name: 'Marcus Whitfield', email: 'marcus.whitfield@cedarridge.example', title: 'Chief Investment Officer' },
  org: 'Cedar Ridge Pension Trust',
  accountId: 'acc-cedar-ridge',
  contactId: 'con-marcus',
  opportunityId: 'opp-cedar-equity',
  match: { method: 'email', confidence: 0.98 },
  mentions: [
    {
      surface: 'Contoso Equity Fund, class C',
      fund: 'Contoso Equity Fund',
      stated: 'C',
      chosen: 'C',
      confidence: 0.97,
      type: 'interest',
      seg: [3, 4],
      candidates: [
        { cls: 'C', score: 0.97, matched: ['fund_name', 'share_class'] },
        { cls: 'A', score: 0.46, matched: ['fund_name'] },
        { cls: 'Advisor', score: 0.41, matched: ['fund_name'] },
      ],
    },
    {
      surface: 'Equity Income Fund, Advisor shares',
      fund: 'Contoso Equity Income Fund',
      stated: 'Advisor',
      chosen: 'Advisor',
      confidence: 0.94,
      type: 'existing_holding',
      seg: [5],
      candidates: [
        { cls: 'Advisor', score: 0.94, matched: ['fund_name', 'share_class'] },
        { cls: 'A', score: 0.44, matched: ['fund_name'] },
        { cls: 'C', score: 0.4, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'fees_expenses', confidence: 0.93, seg: [6, 7] },
    { id: 'share_class_selection', confidence: 0.9, seg: [3] },
    { id: 'equity_strategy', confidence: 0.84, seg: [0, 1] },
    { id: 'asset_allocation', confidence: 0.78, seg: [1] },
  ],
  tasks: [
    { title: 'Send fee schedule and Class C vs Advisor expense comparison to Marcus Whitfield', dueDays: 3, dueText: 'by Friday', basis: 'stated', confidence: 0.92, seg: [7] },
    { title: 'Schedule time with the Cedar Ridge investment committee before the 20th', dueDays: 5, dueText: 'before the twentieth', basis: 'inferred', confidence: 0.84, seg: [8, 9] },
  ],
  facts: {
    overview:
      'Avery Collins followed up with Marcus Whitfield (CIO, Cedar Ridge Pension Trust) on adding U.S. large-cap equity exposure. Marcus asked for Class C specifically and wants its ongoing cost compared with the Advisor class. Avery will send a fee comparison by Friday and arrange an investment committee session.',
    context: [
      'Considering a ~5% increase to U.S. large-cap equity',
      'Investment committee meets on the 20th',
      'Wants a level-load share class for the transition period',
      'Existing sleeve already holds the Equity Income Fund (Advisor class)',
    ],
    concerns: ['Total ongoing cost of Class C versus the Advisor class over a three-year horizon'],
    relationship: [],
  },
  policyActions: [
    { kind: 'pii_redacted', category: 'personal_contact', segment_ids: [10], detail: 'Phone number replaced with [PHONE_NUMBER] before extraction.' },
  ],
  turns: heroTurns,
  flags: ['pricing_discussed'],
  confidence: 0.94,
};

// ---- 2. ambiguous growth: top-3 pick ---------------------------------------------------------------
const ambiguousTurns: Turn[] = [
  S("Hi Helen, thanks for making time. You mentioned the endowment is reviewing its growth allocation."),
  C('Yes. The board wants a bit more growth exposure without adding much volatility.'),
  C('Someone on my team keeps pointing at the Growth Opportunities fund, but I never got which share class they hold.'),
  S('I can check which class the endowment is in. Performance there has been strong over three years.'),
  C('Good. We also compared it with the Small Cap Value Fund, A shares, which feels cheaper.'),
  S('The A shares carry a front-end load, so it depends on the ticket size.'),
  C('Right. Can you send performance and fee details for both?'),
  S("I'll confirm the class you hold and send a performance summary within three days, plus the Small Cap Value fact sheet."),
  C('Perfect. Thanks, Jordan.'),
];

const ambiguous: MockScript = {
  info: info(
    'pb-ambiguous-equity-fund',
    'Ambiguous share class - top-3 pick',
    'The speaker names the fund but never the share class, so the resolver falls back to a top-3 pick list (Class C / Class A / Advisor).',
    'teams',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'discovery', tags: ['ambiguous_product', 'top3', 'teams'], duration_minutes: 9 },
    ambiguousTurns.length,
  ),
  direction: 'meeting',
  seller: sellerJordan,
  customer: { name: 'Helen Okafor', email: 'helen.okafor@harborview.example', title: 'Chief Investment Officer' },
  org: 'Harborview Endowment Fund',
  accountId: 'acc-harborview',
  contactId: 'con-helen',
  opportunityId: 'opp-harbor-income',
  match: { method: 'name_org', confidence: 0.91 },
  mentions: [
    {
      surface: 'the Growth Opportunities fund',
      fund: 'Contoso Growth Opportunities Fund',
      stated: null,
      chosen: null,
      confidence: 0.46,
      type: 'existing_holding',
      seg: [2, 3],
      candidates: [
        { cls: 'C', score: 0.46, matched: ['fund_name'] },
        { cls: 'A', score: 0.44, matched: ['fund_name'] },
        { cls: 'Advisor', score: 0.41, matched: ['fund_name'] },
      ],
      reasons: ['share_class_not_stated', 'low_margin'],
    },
    {
      surface: 'Small Cap Value Fund, A shares',
      fund: 'Contoso Small Cap Value Fund',
      stated: 'A',
      chosen: 'A',
      confidence: 0.95,
      type: 'comparison',
      seg: [4, 5],
      candidates: [
        { cls: 'A', score: 0.95, matched: ['fund_name', 'share_class'] },
        { cls: 'C', score: 0.45, matched: ['fund_name'] },
        { cls: 'Advisor', score: 0.4, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'performance_returns', confidence: 0.88, seg: [3] },
    { id: 'risk_volatility', confidence: 0.8, seg: [1] },
    { id: 'fees_expenses', confidence: 0.83, seg: [4, 5, 6] },
    { id: 'equity_strategy', confidence: 0.75, seg: [1] },
  ],
  tasks: [
    { title: 'Confirm which Growth Opportunities share class Harborview holds and send a performance summary', dueDays: 3, dueText: 'within three days', basis: 'stated', confidence: 0.9, seg: [7] },
    { title: 'Send Contoso Small Cap Value Fund fact sheet and fee details to Helen Okafor', dueDays: null, basis: 'none', confidence: 0.8, seg: [6, 7] },
  ],
  facts: {
    overview:
      "Jordan Reyes met Helen Okafor (CIO, Harborview Endowment Fund) to review the endowment's growth allocation. The board wants more growth exposure without materially higher volatility; the Growth Opportunities fund and the Small Cap Value Fund were compared. Jordan will confirm the held share class and send performance and fee details.",
    context: [
      'Board wants more growth exposure with limited added volatility',
      'Comparing a large-cap growth fund with a small-cap value fund',
      'Ticket size will drive the share-class choice',
    ],
    concerns: ['Front-end load on A shares versus ticket size'],
    relationship: [],
  },
  policyActions: [],
  turns: ambiguousTurns,
  flags: ['pricing_discussed'],
  confidence: 0.71,
};

// ---- 3. Zoom fixed-income follow-up ----------------------------------------------------------------
const bondTurns: Turn[] = [
  S("Daniel, thanks for the time. Let's pick up on the short-duration sleeve."),
  C("We're replacing a maturing sleeve and want something with limited duration risk."),
  C('We like the Core Plus Bond Fund, class I, as the core holding.'),
  S('And for the short end the Short Duration Income Fund, R6 shares, fits the liquidity profile.'),
  C('R6 works for us. What are the redemption terms if cash needs change mid-year?'),
  S("Daily liquidity. I'll send the redemption terms and a benchmark comparison by next Wednesday."),
  C('Please also loop in our operations lead, Sam, on the account setup.'),
  S("Will do. I'll email Sam the account setup checklist tomorrow."),
  C('Thanks, that covers it.'),
];

const bond: MockScript = {
  info: info(
    'pb-zoom-fixed-income-followup',
    'Zoom follow-up - short-duration bond sleeve',
    'Same output shape as Teams from a Zoom recording: two clearly stated products, dated commitments and a third-party follow-up.',
    'zoom',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'follow_up', tags: ['zoom', 'tasks', 'dates'], duration_minutes: 10 },
    bondTurns.length,
  ),
  direction: 'outbound',
  seller: sellerAvery,
  customer: { name: 'Daniel Brooks', email: 'daniel.brooks@lakeshore-trs.example', title: 'Head of Fixed Income' },
  org: "Lakeshore Teachers' Retirement System",
  accountId: 'acc-lakeshore',
  contactId: 'con-daniel',
  opportunityId: 'opp-lakeshore-bond',
  match: { method: 'email', confidence: 0.97 },
  mentions: [
    {
      surface: 'Core Plus Bond Fund, class I',
      fund: 'Contoso Core Plus Bond Fund',
      stated: 'I',
      chosen: 'I',
      confidence: 0.96,
      type: 'interest',
      seg: [2],
      candidates: [
        { cls: 'I', score: 0.96, matched: ['fund_name', 'share_class'] },
        { cls: 'R6', score: 0.5, matched: ['fund_name'] },
        { cls: 'A', score: 0.42, matched: ['fund_name'] },
      ],
    },
    {
      surface: 'Short Duration Income Fund, R6 shares',
      fund: 'Contoso Short Duration Income Fund',
      stated: 'R6',
      chosen: 'R6',
      confidence: 0.95,
      type: 'interest',
      seg: [3, 4],
      candidates: [
        { cls: 'R6', score: 0.95, matched: ['fund_name', 'share_class'] },
        { cls: 'I', score: 0.48, matched: ['fund_name'] },
        { cls: 'A', score: 0.4, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'fixed_income_duration', confidence: 0.92, seg: [1, 2] },
    { id: 'liquidity_redemptions', confidence: 0.9, seg: [4, 5] },
    { id: 'benchmarks_comparison', confidence: 0.8, seg: [5] },
    { id: 'onboarding_service', confidence: 0.7, seg: [6, 7] },
  ],
  tasks: [
    { title: 'Send redemption terms and a benchmark comparison to Daniel Brooks', dueDays: 6, dueText: 'by next Wednesday', basis: 'stated', confidence: 0.93, seg: [5] },
    { title: 'Email the account setup checklist to Sam (Lakeshore operations)', dueDays: 1, dueText: 'tomorrow', basis: 'stated', confidence: 0.9, seg: [7] },
  ],
  facts: {
    overview:
      'Avery Collins followed up with Daniel Brooks (Head of Fixed Income, Lakeshore Teachers\u2019 Retirement System) on replacing a maturing bond sleeve with limited duration risk. Core Plus Bond (Class I) and Short Duration Income (Class R6) were agreed as the shortlist. Avery will send redemption terms and benchmark comparisons.',
    context: ['Replacing a maturing sleeve; wants limited duration risk', 'Daily liquidity matters if cash needs change mid-year', 'Operations lead (Sam) to be looped into account setup'],
    concerns: ['Redemption terms if cash needs change mid-year'],
    relationship: [],
  },
  policyActions: [],
  turns: bondTurns,
  confidence: 0.95,
};

// ---- 4. Genesys inbound service call ---------------------------------------------------------------
const serviceTurns: Turn[] = [
  C("Hi, this is Priya at Cedar Ridge. I'm calling about our September statement."),
  S("Happy to help, Priya. What looks off?"),
  C('The Equity Fund Class A position shows a different share count than our records.'),
  S('Thanks. Let me check the transfer-agent record for the Equity Fund Class A account.'),
  C('Also, we may move that position into Class I once the new mandate is approved. Could you outline what is needed?'),
  S("Yes, a share class conversion request. I'll email the form and a short guide."),
  S("I'll also reissue the September statement by tomorrow."),
  C('Perfect, thank you.'),
];

const service: MockScript = {
  info: info(
    'pb-genesys-inbound-service',
    'Genesys inbound - statement and share class conversion',
    'Genesys-format transcript of an inbound service call: an existing Class A holding plus a possible conversion to Class I.',
    'genesys',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'service', tags: ['genesys', 'inbound', 'service'], duration_minutes: 6 },
    serviceTurns.length,
  ),
  direction: 'inbound',
  seller: sellerMorgan,
  customer: { name: 'Priya Nair', email: 'priya.nair@cedarridge.example', title: 'Investment Analyst' },
  org: 'Cedar Ridge Pension Trust',
  accountId: 'acc-cedar-ridge',
  contactId: 'con-priya',
  opportunityId: null,
  match: { method: 'meeting_metadata', confidence: 0.93 },
  mentions: [
    {
      surface: 'Equity Fund Class A',
      fund: 'Contoso Equity Fund',
      stated: 'A',
      chosen: 'A',
      confidence: 0.96,
      type: 'existing_holding',
      seg: [2, 3],
      candidates: [
        { cls: 'A', score: 0.96, matched: ['fund_name', 'share_class'] },
        { cls: 'C', score: 0.44, matched: ['fund_name'] },
        { cls: 'I', score: 0.4, matched: ['fund_name'] },
      ],
    },
    {
      surface: 'Class I',
      fund: 'Contoso Equity Fund',
      stated: 'I',
      chosen: 'I',
      confidence: 0.82,
      type: 'interest',
      seg: [4, 5],
      candidates: [
        { cls: 'I', score: 0.82, matched: ['share_class', 'context_fund'] },
        { cls: 'A', score: 0.55, matched: ['context_fund'] },
        { cls: 'R6', score: 0.4, matched: ['context_fund'] },
      ],
    },
  ],
  topics: [
    { id: 'onboarding_service', confidence: 0.93, seg: [0, 2, 3] },
    { id: 'share_class_selection', confidence: 0.87, seg: [4, 5] },
  ],
  tasks: [
    { title: 'Reissue the September statement to Priya Nair', dueDays: 1, dueText: 'by tomorrow', basis: 'stated', confidence: 0.91, seg: [6] },
    { title: 'Email the share class conversion form and guide to Priya Nair', dueDays: null, basis: 'none', confidence: 0.85, seg: [5] },
  ],
  facts: {
    overview:
      'Priya Nair (Cedar Ridge Pension Trust) called the service desk about a share count difference on the Equity Fund Class A position and asked how a later conversion to Class I would work. The desk will reissue the September statement and send the conversion form.',
    context: ['Statement share count differs from the client\u2019s records', 'New mandate pending; possible Class A to Class I conversion'],
    concerns: ['Share count discrepancy on the September statement'],
    relationship: [],
  },
  policyActions: [],
  turns: serviceTurns,
  confidence: 0.92,
};

// ---- 5. Spanish Zoom call, ambiguous CRM contact ---------------------------------------------------
const spanishTurns: Turn[] = [
  S('Buenos días, Sofía. Gracias por reunirse con nosotros para hablar del mandato de renta variable global.', 'es-US'),
  C('Buenos días, Avery. El comité quiere aumentar la exposición a renta variable internacional, pero sin descuidar los costos.', 'es-US'),
  C('Nos interesa el fondo de renta variable global, clase C, para empezar con una posición pequeña.', 'es-US'),
  S('Perfecto. La clase C tiene una comisión nivelada, y puedo enviarle la comparación con otras clases.', 'es-US'),
  C('También estamos mirando Woodgrove Mercados Emergentes, clase I, aunque nos preocupa la volatilidad.', 'es-US'),
  S('Entendido. Puedo preparar un análisis de riesgo y volatilidad de mercados emergentes.', 'es-US'),
  C('Además, queremos saber cómo incorporan criterios de sostenibilidad en la selección.', 'es-US'),
  S('Le enviaré el documento de política ASG y la presentación en español el viernes.', 'es-US'),
  C('Muy bien. Hablamos la próxima semana para fijar fecha con el comité.', 'es-US'),
];

const spanish: MockScript = {
  info: info(
    'pb-zoom-es-mandato-renta-variable',
    'Spanish - mandato de renta variable global',
    'A Spanish-language Zoom call: products with share class, topics and tasks come out in English CRM terms; two similar contact names trigger the ambiguous CRM match warning.',
    'zoom',
    { language: 'es-US', market: 'US', business_line: 'professional_buyer', call_type: 'discovery', tags: ['spanish', 'zoom', 'crm_ambiguous'], duration_minutes: 11 },
    spanishTurns.length,
  ),
  direction: 'outbound',
  seller: sellerAvery,
  customer: { name: 'Sofía Marín', email: 'sofia.marin@alameda-fp.example', title: 'Directora de Inversiones' },
  org: 'Fondo de Pensiones Alameda',
  accountId: 'acc-alameda',
  contactId: 'con-sofia',
  opportunityId: 'opp-alameda-global',
  match: { method: 'name_org', confidence: 0.62, ambiguous: true, candidateContactIds: ['con-sofia', 'con-sofia-v'] },
  mentions: [
    {
      surface: 'fondo de renta variable global, clase C',
      fund: 'Contoso Global Equity Fund',
      stated: 'C',
      chosen: 'C',
      confidence: 0.95,
      type: 'interest',
      seg: [2, 3],
      candidates: [
        { cls: 'C', score: 0.95, matched: ['alias_es', 'share_class'] },
        { cls: 'A', score: 0.47, matched: ['alias_es'] },
        { cls: 'Advisor', score: 0.4, matched: ['alias_es'] },
      ],
    },
    {
      surface: 'Woodgrove Mercados Emergentes, clase I',
      fund: 'Woodgrove Emerging Markets Equity Fund',
      stated: 'I',
      chosen: 'I',
      confidence: 0.91,
      type: 'interest',
      seg: [4, 5],
      candidates: [
        { cls: 'I', score: 0.91, matched: ['alias_es', 'share_class'] },
        { cls: 'R6', score: 0.5, matched: ['alias_es'] },
        { cls: 'A', score: 0.41, matched: ['alias_es'] },
      ],
    },
  ],
  topics: [
    { id: 'asset_allocation', confidence: 0.86, seg: [1] },
    { id: 'emerging_markets', confidence: 0.9, seg: [4, 5] },
    { id: 'esg_sustainability', confidence: 0.88, seg: [6, 7] },
    { id: 'fees_expenses', confidence: 0.8, seg: [1, 3] },
    { id: 'risk_volatility', confidence: 0.82, seg: [4, 5] },
  ],
  tasks: [
    { title: 'Send emerging markets risk and volatility analysis to Sofía Marín', dueDays: null, basis: 'none', confidence: 0.84, seg: [5] },
    { title: 'Send ESG policy document and Spanish-language presentation to Sofía Marín', dueDays: 3, dueText: 'el viernes', basis: 'stated', confidence: 0.9, seg: [7] },
    { title: 'Agree an investment committee date with Alameda next week', dueDays: 7, dueText: 'la próxima semana', basis: 'inferred', confidence: 0.8, seg: [8] },
  ],
  facts: {
    overview:
      'Avery Collins met Sofía Marín (Directora de Inversiones, Fondo de Pensiones Alameda) in Spanish about a global equity mandate. The committee wants more international equity exposure with attention to cost; interest centred on the Global Equity Fund Class C and Woodgrove Emerging Markets Equity Fund Class I, with volatility and ESG questions.',
    context: ['Committee wants to raise international equity exposure', 'Starting with a small Class C position', 'Volatility concern on emerging markets', 'Asks how sustainability criteria enter manager selection'],
    concerns: ['Volatility of emerging-markets exposure', 'Cost sensitivity'],
    relationship: [],
  },
  policyActions: [],
  turns: spanishTurns,
  confidence: 0.9,
};

// ---- 6. privacy: card, medical, football, Seattle fan ----------------------------------------------
const privacyTurns: Turn[] = [
  C("Hi Morgan, it's Jordan Ellis from Evergreen Advisory."),
  S('Jordan, great to hear from you. How are things in Seattle?'),
  C('Busy! Did you catch the game on Sunday?'),
  S('I did, what a finish.'),
  C("Seahawks all the way, I haven't missed a game in years. Final score was twenty-four to twenty-one, can you believe it?"),
  S('Ha, I believe it. Right, how can I help today?'),
  C("I'm updating a model portfolio for retirement-income clients and want to look at the Balanced Allocation Fund, Advisor shares."),
  S('That fits. Are you also looking at an equity sleeve?'),
  C('Yes, the Sustainable Equity Fund, Advisor shares, for clients who ask about ESG.'),
  C("Before we go further, I should say I've been out for a while."),
  C(''),
  S('Of course, no rush on anything.'),
  C(''),
  S("Understood. Let's keep today's items simple."),
  C('To pay the conference registration fee my card number is [PAYMENT_CARD], expiry [DATE].'),
  C('And my direct line is [PHONE_NUMBER].'),
  S("Thanks. I'll send the updated model portfolio next Thursday."),
  C('Email is best for follow-ups, I rarely pick up the phone.'),
  S('Noted. Talk soon, Jordan.'),
];

const privacy: MockScript = {
  info: info(
    'rt-genesys-privacy-seahawks',
    'Privacy - card, medical, football, Seattle fan',
    "A card number, a medical disclosure and football chat. The first three never reach the note; the 'Seattle fan' relationship fact is kept (retail template).",
    'genesys',
    { language: 'en-US', market: 'US', business_line: 'retail', call_type: 'relationship', tags: ['privacy', 'relationship', 'small_talk', 'genesys'], duration_minutes: 8 },
    privacyTurns.length,
  ),
  direction: 'inbound',
  seller: sellerMorgan,
  customer: { name: 'Jordan Ellis', email: 'jordan.ellis@evergreenadvisory.example', title: 'Senior Advisor' },
  org: 'Evergreen Advisory Group',
  accountId: 'acc-evergreen',
  contactId: 'con-jordan-e',
  opportunityId: 'opp-evergreen-models',
  match: { method: 'email', confidence: 0.99 },
  mentions: [
    {
      surface: 'Balanced Allocation Fund, Advisor shares',
      fund: 'Contoso Balanced Allocation Fund',
      stated: 'Advisor',
      chosen: 'Advisor',
      confidence: 0.93,
      type: 'interest',
      seg: [6, 7],
      candidates: [
        { cls: 'Advisor', score: 0.93, matched: ['fund_name', 'share_class'] },
        { cls: 'A', score: 0.45, matched: ['fund_name'] },
        { cls: 'I', score: 0.4, matched: ['fund_name'] },
      ],
    },
    {
      surface: 'Sustainable Equity Fund, Advisor shares',
      fund: 'Contoso Sustainable Equity Fund',
      stated: 'Advisor',
      chosen: 'Advisor',
      confidence: 0.92,
      type: 'interest',
      seg: [8],
      candidates: [
        { cls: 'Advisor', score: 0.92, matched: ['fund_name', 'share_class'] },
        { cls: 'A', score: 0.44, matched: ['fund_name'] },
        { cls: 'C', score: 0.4, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'retirement_planning', confidence: 0.86, seg: [6] },
    { id: 'asset_allocation', confidence: 0.84, seg: [6, 7] },
    { id: 'esg_sustainability', confidence: 0.85, seg: [8] },
    { id: 'platform_access', confidence: 0.6, seg: [6] },
  ],
  tasks: [{ title: 'Send the updated model portfolio to Jordan Ellis', dueDays: 9, dueText: 'next Thursday', basis: 'stated', confidence: 0.9, seg: [16] }],
  facts: {
    overview:
      'Morgan Patel spoke with Jordan Ellis (Senior Advisor, Evergreen Advisory Group) about refreshing a retirement-income model portfolio. Jordan is considering the Balanced Allocation Fund and the Sustainable Equity Fund, both Advisor class. Morgan will send the updated model portfolio next Thursday.',
    context: ['Updating a model portfolio for retirement-income clients', 'Some clients ask for ESG-oriented equity exposure'],
    concerns: [],
    relationship: ['Longtime Seattle Seahawks fan', 'Prefers email over phone for follow-ups'],
  },
  policyActions: [
    { kind: 'dropped_small_talk', category: 'sports', segment_ids: [2, 3, 5], detail: 'Sports chat (game result and score) dropped from the note: not business relevant.' },
    { kind: 'kept_relationship', category: 'interest', segment_ids: [4], detail: 'Kept as a relationship fact: supports the Seattle Seahawks (score detail dropped).' },
    { kind: 'omitted_sensitive', category: 'medical', segment_ids: [10], detail: 'Health disclosure omitted from the note, the working transcript and every later stage.' },
    { kind: 'omitted_sensitive', category: 'family_health', segment_ids: [12], detail: 'Family health comment omitted.' },
    { kind: 'pii_redacted', category: 'payment_card', segment_ids: [14], detail: 'Card number and expiry replaced with [PAYMENT_CARD] / [DATE] before any model saw the text.' },
    { kind: 'pii_redacted', category: 'personal_contact', segment_ids: [15], detail: 'Phone number replaced with [PHONE_NUMBER].' },
    { kind: 'kept_relationship', category: 'preference', segment_ids: [17], detail: 'Kept as a relationship fact: prefers email for follow-ups.' },
  ],
  turns: privacyTurns,
  confidence: 0.92,
};

// ---- 7. mobile in-person visit ---------------------------------------------------------------------
const visitTurns: Turn[] = [
  S('Daniel, thanks for hosting me at your office.'),
  C("Glad you could come. Let's talk about the municipal exposure."),
  C("We hold the Municipal Income Fund, class A, and we're considering adding the Tax-Free Intermediate Fund, class I."),
  S("Makes sense for a tax-aware sleeve. I'll send a duration comparison."),
  C('Please include the after-tax yield on both.'),
  S('Will do by Monday.'),
];

const visit: MockScript = {
  info: info(
    'pb-mobile-inperson-visit',
    'In-person visit recorded on a phone',
    'Audio-only capture from the mobile web app (recorded offline, uploaded later) producing the same note, products and tasks.',
    'mobile',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'discovery', tags: ['mobile', 'offline', 'audio_only'], duration_minutes: 7 },
    visitTurns.length,
  ),
  direction: 'in_person',
  seller: sellerJordan,
  customer: { name: 'Daniel Brooks', email: 'daniel.brooks@lakeshore-trs.example', title: 'Head of Fixed Income' },
  org: "Lakeshore Teachers' Retirement System",
  accountId: 'acc-lakeshore',
  contactId: 'con-daniel',
  opportunityId: 'opp-lakeshore-bond',
  match: { method: 'manual', confidence: 0.9 },
  mentions: [
    {
      surface: 'Municipal Income Fund, class A',
      fund: 'Contoso Municipal Income Fund',
      stated: 'A',
      chosen: 'A',
      confidence: 0.93,
      type: 'existing_holding',
      seg: [2],
      candidates: [
        { cls: 'A', score: 0.93, matched: ['fund_name', 'share_class'] },
        { cls: 'C', score: 0.45, matched: ['fund_name'] },
        { cls: 'I', score: 0.41, matched: ['fund_name'] },
      ],
    },
    {
      surface: 'Tax-Free Intermediate Fund, class I',
      fund: 'Contoso Tax-Free Intermediate Fund',
      stated: 'I',
      chosen: 'I',
      confidence: 0.9,
      type: 'interest',
      seg: [2],
      candidates: [
        { cls: 'I', score: 0.9, matched: ['fund_name', 'share_class'] },
        { cls: 'A', score: 0.46, matched: ['fund_name'] },
        { cls: 'R6', score: 0.4, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'tax_efficiency', confidence: 0.9, seg: [1, 3] },
    { id: 'fixed_income_duration', confidence: 0.82, seg: [3] },
  ],
  tasks: [{ title: 'Send duration and after-tax yield comparison to Daniel Brooks', dueDays: 5, dueText: 'by Monday', basis: 'stated', confidence: 0.9, seg: [3, 4, 5] }],
  facts: {
    overview:
      'Jordan Reyes visited Daniel Brooks (Head of Fixed Income, Lakeshore) in person to discuss municipal exposure. Lakeshore holds the Municipal Income Fund Class A and is considering the Tax-Free Intermediate Fund Class I. Jordan will send a duration and after-tax yield comparison by Monday.',
    context: ['Tax-aware municipal sleeve', 'Existing Class A holding; evaluating a Class I add'],
    concerns: [],
    relationship: [],
  },
  policyActions: [],
  turns: visitTurns,
  confidence: 0.9,
};

// ---- 8. near-identical fund names ------------------------------------------------------------------
const identicalTurns: Turn[] = [
  C('Hello, I want to place an order for the Equity Opportunities Fund, Advisor shares, not the Equity Fund.'),
  S('Understood, Equity Opportunities Fund, Advisor shares. Which account?'),
  C('The Cedar Ridge main trust account.'),
  S("Great, I'll confirm the order details by email today."),
  C('Thanks.'),
];

const identical: MockScript = {
  info: info(
    'pb-genesys-near-identical-names',
    'Near-identical fund names',
    'Equity Fund vs Equity Income Fund vs Equity Opportunities Fund: confusable names with the share class stated; the candidate list spans different funds.',
    'genesys',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'service', tags: ['near_identical_names', 'class_trap', 'genesys'], duration_minutes: 4 },
    identicalTurns.length,
  ),
  direction: 'inbound',
  seller: sellerMorgan,
  customer: { name: 'Priya Nair', email: 'priya.nair@cedarridge.example', title: 'Investment Analyst' },
  org: 'Cedar Ridge Pension Trust',
  accountId: 'acc-cedar-ridge',
  contactId: 'con-priya',
  opportunityId: null,
  match: { method: 'email', confidence: 0.96 },
  mentions: [
    {
      surface: 'Equity Opportunities Fund, Advisor shares',
      fund: 'Contoso Equity Opportunities Fund',
      stated: 'Advisor',
      chosen: 'Advisor',
      confidence: 0.9,
      type: 'interest',
      seg: [0, 1],
      candidates: [
        { cls: 'Advisor', score: 0.9, matched: ['fund_name', 'share_class'] },
        { cls: 'Advisor', score: 0.62, matched: ['fund_name_partial', 'share_class'], fund: 'Contoso Equity Fund' },
        { cls: 'Advisor', score: 0.6, matched: ['fund_name_partial', 'share_class'], fund: 'Contoso Equity Income Fund' },
      ],
    },
  ],
  topics: [
    { id: 'onboarding_service', confidence: 0.85, seg: [0, 2] },
    { id: 'share_class_selection', confidence: 0.8, seg: [0, 1] },
  ],
  tasks: [{ title: 'Email order confirmation for the Equity Opportunities Fund Advisor class to Priya Nair', dueDays: 0, dueText: 'today', basis: 'stated', confidence: 0.9, seg: [3] }],
  facts: {
    overview: 'Priya Nair (Cedar Ridge Pension Trust) placed an order request for the Equity Opportunities Fund Advisor class and explicitly excluded the Equity Fund. The desk will confirm the order details by email today.',
    context: ['Order placed for the main trust account'],
    concerns: [],
    relationship: [],
  },
  policyActions: [],
  turns: identicalTurns,
  confidence: 0.9,
};

// ---- 9. UK Teams call (transcript retained per market policy) --------------------------------------
const ukTurns: Turn[] = [
  S('Oliver, thanks for joining from London.'),
  C('Thanks, Avery. The trustees are reviewing the growth portfolio and want to look at multi-asset funds.'),
  C('The Strategic Multi-Asset Fund, class I, is on our shortlist.'),
  S("That's a good fit for the liability-aware allocation you described."),
  C("We're also weighing the Global Fixed Income Fund, R6 shares, as a diversifier."),
  S('Understood. I can send a side-by-side with fees and risk.'),
  C('Please do, and set up a session with the trustees in two weeks.'),
  S("I'll arrange it and send the pack by next Tuesday."),
];

const uk: MockScript = {
  info: info(
    'int-teams-uk-multi-asset',
    'UK market - multi-asset allocation (transcript retained)',
    'UK policy retains a redacted transcript in the vault for a configurable period; flip the market toggle to see the transcript panel appear or vanish. Dataverse never holds it.',
    'teams',
    { language: 'en-GB', market: 'UK', business_line: 'international', call_type: 'discovery', tags: ['market_policy', 'uk', 'teams'], duration_minutes: 9 },
    ukTurns.length,
  ),
  direction: 'meeting',
  seller: sellerAvery,
  customer: { name: 'Oliver Hartley', email: 'oliver.hartley@highgate-ps.example', title: 'Head of Investments' },
  org: 'Highgate Pension Scheme',
  accountId: 'acc-highgate',
  contactId: 'con-oliver',
  opportunityId: 'opp-highgate-multi',
  match: { method: 'email', confidence: 0.97 },
  mentions: [
    {
      surface: 'Strategic Multi-Asset Fund, class I',
      fund: 'Contoso Strategic Multi-Asset Fund',
      stated: 'I',
      chosen: 'I',
      confidence: 0.95,
      type: 'interest',
      seg: [2, 3],
      candidates: [
        { cls: 'I', score: 0.95, matched: ['fund_name', 'share_class'] },
        { cls: 'R6', score: 0.5, matched: ['fund_name'] },
        { cls: 'A', score: 0.42, matched: ['fund_name'] },
      ],
    },
    {
      surface: 'Global Fixed Income Fund, R6 shares',
      fund: 'Woodgrove Global Fixed Income Fund',
      stated: 'R6',
      chosen: 'R6',
      confidence: 0.93,
      type: 'interest',
      seg: [4, 5],
      candidates: [
        { cls: 'R6', score: 0.93, matched: ['fund_name', 'share_class'] },
        { cls: 'I', score: 0.49, matched: ['fund_name'] },
        { cls: 'A', score: 0.41, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'asset_allocation', confidence: 0.9, seg: [1, 3] },
    { id: 'risk_volatility', confidence: 0.8, seg: [5] },
    { id: 'fees_expenses', confidence: 0.75, seg: [5] },
  ],
  tasks: [
    { title: 'Send side-by-side fees and risk comparison to Oliver Hartley', dueDays: 6, dueText: 'by next Tuesday', basis: 'stated', confidence: 0.91, seg: [5, 7] },
    { title: 'Schedule a trustee session at Highgate in two weeks', dueDays: 14, dueText: 'in two weeks', basis: 'stated', confidence: 0.86, seg: [6, 7] },
  ],
  facts: {
    overview:
      'Avery Collins met Oliver Hartley (Head of Investments, Highgate Pension Scheme). The trustees are reviewing the growth portfolio and shortlisted the Strategic Multi-Asset Fund (Class I) and the Global Fixed Income Fund (Class R6). Avery will send a fee and risk comparison and arrange a trustee session.',
    context: ['Trustees reviewing the growth portfolio', 'Liability-aware allocation under discussion', 'Fixed income considered as a diversifier'],
    concerns: [],
    relationship: [],
  },
  policyActions: [],
  turns: ukTurns,
  confidence: 0.93,
};

// ---- 10. Spanish mobile retail visit ---------------------------------------------------------------
const esVisitTurns: Turn[] = [
  S('Amelia, gracias por recibirme hoy.', 'es-US'),
  C('Con gusto. Estoy actualizando las carteras de mis clientes que se jubilan pronto.', 'es-US'),
  C('Me interesa el Fondo de Dividendos Globales, clase A, para los ingresos.', 'es-US'),
  S('Buena opción. Le envío la ficha técnica y una comparación con la clase Advisor.', 'es-US'),
  C('Por cierto, soy aficionada al béisbol y sigo a los Marineros.', 'es-US'),
  S('Lo recordaré. Le escribo mañana con los documentos.', 'es-US'),
];

const esVisit: MockScript = {
  info: info(
    'rt-mobile-es-advisor-visit',
    'Spanish in-person advisor visit (retail)',
    'Spanish audio captured on a phone: product and share class in Spanish, a relationship fact kept, English retail note.',
    'mobile',
    { language: 'es-US', market: 'US', business_line: 'retail', call_type: 'relationship', tags: ['spanish', 'mobile', 'offline', 'relationship'], duration_minutes: 6 },
    esVisitTurns.length,
  ),
  direction: 'in_person',
  seller: sellerMorgan,
  customer: { name: 'Amelia Torres', email: 'amelia.torres@summitpeak.example', title: 'Financial Advisor' },
  org: 'Summit Peak Wealth Partners',
  accountId: 'acc-summit',
  contactId: 'con-amelia',
  opportunityId: null,
  match: { method: 'manual', confidence: 0.9 },
  mentions: [
    {
      surface: 'Fondo de Dividendos Globales, clase A',
      fund: 'Contoso Global Dividend Fund',
      stated: 'A',
      chosen: 'A',
      confidence: 0.94,
      type: 'interest',
      seg: [2, 3],
      candidates: [
        { cls: 'A', score: 0.94, matched: ['alias_es', 'share_class'] },
        { cls: 'Advisor', score: 0.5, matched: ['alias_es'] },
        { cls: 'C', score: 0.41, matched: ['alias_es'] },
      ],
    },
  ],
  topics: [
    { id: 'retirement_planning', confidence: 0.88, seg: [1, 2] },
    { id: 'asset_allocation', confidence: 0.72, seg: [1] },
  ],
  tasks: [{ title: 'Send Global Dividend Fund fact sheet and a Class A vs Advisor comparison to Amelia Torres', dueDays: 1, dueText: 'mañana', basis: 'stated', confidence: 0.9, seg: [3, 5] }],
  facts: {
    overview:
      'Morgan Patel visited Amelia Torres (Financial Advisor, Summit Peak Wealth Partners) in person. Amelia is updating portfolios for clients nearing retirement and is interested in the Global Dividend Fund Class A for income. Morgan will send the fact sheet and a class comparison tomorrow.',
    context: ['Updating portfolios for clients nearing retirement', 'Income focus'],
    concerns: [],
    relationship: ['Baseball fan - follows the Seattle Mariners'],
  },
  policyActions: [{ kind: 'kept_relationship', category: 'interest', segment_ids: [4], detail: 'Kept as a relationship fact: baseball fan (Seattle Mariners).' }],
  turns: esVisitTurns,
  confidence: 0.91,
};

// ---- 11. EU Zoom call ------------------------------------------------------------------------------
const euTurns: Turn[] = [
  S("Freja, thanks for the time. Let's continue the income allocation discussion."),
  C('We want more income but within our risk budget.'),
  C("The Global Dividend Fund, class I, and the Floating Rate Fund, R6 shares, are the two we'd like to compare."),
  S("I'll prepare a yield and drawdown comparison for both."),
  C('Please add the sustainability classification, our regulator asks about it.'),
  S("Understood. You'll have it by the end of next week."),
];

const eu: MockScript = {
  info: info(
    'int-zoom-eu-income-allocation',
    'EU market - income allocation (five-year transcript retention)',
    'EU example policy keeps the redacted transcript for five years in the vault; compare with the US summary-only market.',
    'zoom',
    { language: 'en-GB', market: 'EU', business_line: 'international', call_type: 'follow_up', tags: ['market_policy', 'eu', 'zoom'], duration_minutes: 8 },
    euTurns.length,
  ),
  direction: 'outbound',
  seller: sellerJordan,
  customer: { name: 'Freja Lindqvist', email: 'freja.lindqvist@nordlys.example', title: 'Portfolio Manager' },
  org: 'Nordlys Pension Fund',
  accountId: 'acc-nordlys',
  contactId: 'con-freja',
  opportunityId: null,
  match: { method: 'email', confidence: 0.96 },
  mentions: [
    {
      surface: 'Global Dividend Fund, class I',
      fund: 'Contoso Global Dividend Fund',
      stated: 'I',
      chosen: 'I',
      confidence: 0.94,
      type: 'interest',
      seg: [2],
      candidates: [
        { cls: 'I', score: 0.94, matched: ['fund_name', 'share_class'] },
        { cls: 'R6', score: 0.5, matched: ['fund_name'] },
        { cls: 'A', score: 0.41, matched: ['fund_name'] },
      ],
    },
    {
      surface: 'Floating Rate Fund, R6 shares',
      fund: 'Contoso Floating Rate Fund',
      stated: 'R6',
      chosen: 'R6',
      confidence: 0.92,
      type: 'comparison',
      seg: [2],
      candidates: [
        { cls: 'R6', score: 0.92, matched: ['fund_name', 'share_class'] },
        { cls: 'I', score: 0.49, matched: ['fund_name'] },
        { cls: 'A', score: 0.4, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'asset_allocation', confidence: 0.85, seg: [1] },
    { id: 'esg_sustainability', confidence: 0.8, seg: [4] },
    { id: 'regulatory_compliance', confidence: 0.75, seg: [4] },
    { id: 'risk_volatility', confidence: 0.7, seg: [1, 3] },
  ],
  tasks: [{ title: 'Send yield, drawdown and sustainability classification comparison to Freja Lindqvist', dueDays: 11, dueText: 'by the end of next week', basis: 'stated', confidence: 0.88, seg: [3, 4, 5] }],
  facts: {
    overview:
      'Jordan Reyes continued the income allocation discussion with Freja Lindqvist (Portfolio Manager, Nordlys Pension Fund). Nordlys wants more income inside its risk budget and will compare the Global Dividend Fund (Class I) with the Floating Rate Fund (Class R6), including sustainability classification.',
    context: ['More income required within the risk budget', 'Regulator asks about sustainability classification'],
    concerns: [],
    relationship: [],
  },
  policyActions: [],
  turns: euTurns,
  confidence: 0.92,
};

// ---- 12. commitments without dates -----------------------------------------------------------------
const commitTurns: Turn[] = [
  S('Priya, thanks for the quick catch-up.'),
  C('Sure. We still need a view on how the Dividend Growth Fund, class R6, behaved in the last drawdown.'),
  S("I'll follow up with you on that."),
  C('And someone should introduce us to the portfolio manager at some point.'),
  S("I'll put that on my list."),
  C('Good. No rush on either.'),
];

const commitments: MockScript = {
  info: info(
    'pb-teams-commitments-no-dates',
    'Commitments with no stated dates',
    "Tasks are created from 'I'll follow up' commitments even when nobody states a due date; due date stays empty rather than invented.",
    'teams',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'follow_up', tags: ['tasks', 'no_due_date', 'teams'], duration_minutes: 5 },
    commitTurns.length,
  ),
  direction: 'meeting',
  seller: sellerAvery,
  customer: { name: 'Priya Nair', email: 'priya.nair@cedarridge.example', title: 'Investment Analyst' },
  org: 'Cedar Ridge Pension Trust',
  accountId: 'acc-cedar-ridge',
  contactId: 'con-priya',
  opportunityId: 'opp-cedar-equity',
  match: { method: 'email', confidence: 0.97 },
  mentions: [
    {
      surface: 'Dividend Growth Fund, class R6',
      fund: 'Contoso Dividend Growth Fund',
      stated: 'R6',
      chosen: 'R6',
      confidence: 0.92,
      type: 'interest',
      seg: [1],
      candidates: [
        { cls: 'R6', score: 0.92, matched: ['fund_name', 'share_class'] },
        { cls: 'I', score: 0.5, matched: ['fund_name'] },
        { cls: 'A', score: 0.41, matched: ['fund_name'] },
      ],
    },
  ],
  topics: [
    { id: 'risk_volatility', confidence: 0.85, seg: [1] },
    { id: 'manager_research', confidence: 0.8, seg: [3] },
  ],
  tasks: [
    { title: 'Follow up with Priya Nair on Dividend Growth Fund R6 drawdown behaviour', dueDays: null, basis: 'none', confidence: 0.86, seg: [1, 2] },
    { title: 'Introduce Cedar Ridge to the Dividend Growth Fund portfolio manager', dueDays: null, basis: 'none', confidence: 0.8, seg: [3, 4] },
  ],
  facts: {
    overview:
      'Avery Collins caught up with Priya Nair (Cedar Ridge Pension Trust). Priya wants to understand how the Dividend Growth Fund Class R6 behaved in the last drawdown and asked for an introduction to the portfolio manager. No due dates were agreed.',
    context: ['Interest in drawdown behaviour of the Dividend Growth Fund (R6)'],
    concerns: ['Behaviour in the last drawdown'],
    relationship: [],
  },
  policyActions: [],
  turns: commitTurns,
  confidence: 0.9,
};

// ---- 13. legacy fund name: the "improves over time" demo ---------------------------------------------
const legacyTurns: Turn[] = [
  S('Helen, thanks for making time ahead of the year-end rebalance.'),
  C("Of course. Before we trim anything I want to look at our holding in the Blue Chip Fund, class C."),
  S('Understood. What size of reduction is the board considering?'),
  C("Possibly a meaningful amount of the class C position. How does a redemption work at that size?"),
  S("I'll confirm the redemption process with our trading desk by Wednesday."),
  C('Please also send the trailing returns and the latest holdings by the end of the week.'),
  S('Will do. Both will be with you by Friday.'),
];

const legacy: MockScript = {
  info: info(
    'pb-teams-legacy-blue-chip',
    'Legacy fund name: teach the alias',
    "The client uses a former fund name that shares no words with the catalogue, so the resolver offers no candidates. Search the catalogue and pick the product; because the class was stated, the phrase is learned for next time.",
    'teams',
    { language: 'en-US', market: 'US', business_line: 'professional_buyer', call_type: 'follow_up', tags: ['legacy_name', 'alias_learning', 'existing_holding', 'teams'], duration_minutes: 8 },
    legacyTurns.length,
  ),
  direction: 'meeting',
  seller: sellerJordan,
  customer: { name: 'Helen Okafor', email: 'helen.okafor@harborview.example', title: 'Chief Investment Officer' },
  org: 'Harborview Endowment Fund',
  accountId: 'acc-harborview',
  contactId: 'con-helen',
  opportunityId: 'opp-harbor-income',
  match: { method: 'email', confidence: 0.97 },
  mentions: [
    {
      surface: 'the Blue Chip Fund, class C',
      fund: 'Contoso Equity Fund',
      stated: 'C',
      chosen: null,
      confidence: 0,
      type: 'existing_holding',
      seg: [1, 3],
      candidates: [],
      reasons: ['no_candidate'],
    },
  ],
  topics: [
    { id: 'performance_returns', confidence: 0.82, seg: [5] },
    { id: 'fees_expenses', confidence: 0.7, seg: [3] },
  ],
  tasks: [
    { title: 'Confirm the redemption process for a class C reduction with the trading desk', dueDays: 4, dueText: 'by Wednesday', basis: 'stated', confidence: 0.9, seg: [4] },
    { title: 'Send trailing returns and latest holdings to Helen Okafor', dueDays: 6, dueText: 'by Friday', basis: 'stated', confidence: 0.9, seg: [5, 6] },
  ],
  facts: {
    overview:
      'Jordan Reyes spoke with Helen Okafor (CIO, Harborview Endowment Fund) ahead of the year-end rebalance. Harborview holds class C shares of a fund it still calls the Blue Chip Fund and may trim the position. Jordan will confirm the redemption process and send trailing returns and holdings.',
    context: ['Year-end rebalance under review', 'Existing class C holding may be reduced'],
    concerns: ['How a redemption at that size would work'],
    relationship: [],
  },
  policyActions: [],
  turns: legacyTurns,
  confidence: 0.6,
};

export const SCRIPTS: MockScript[] = [hero, ambiguous, bond, service, spanish, privacy, visit, identical, uk, esVisit, eu, commitments, legacy];

const BY_FIXTURE = new Map(SCRIPTS.map((script) => [script.info.fixture_id, script]));

export function scriptByFixture(fixtureId: string): MockScript | undefined {
  return BY_FIXTURE.get(fixtureId);
}

export function fixtureInfos(): FixtureInfo[] {
  return SCRIPTS.map((script) => ({ ...script.info, tags: [...script.info.tags] }));
}
