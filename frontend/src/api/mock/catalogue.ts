/** Mock product catalogue: SYNTHETIC and illustrative (fund names are invented, ids are SYN-xxxxxx). */
import type { CatalogueStats, ProductCandidate, ProductSearchHit } from '../types';

export interface MockProduct {
  product_id: string;
  family_id: string;
  fund_name: string;
  share_class: string;
  display_name: string;
  asset_class: string;
  strategy: string;
  region: string;
  vehicle: string;
}

type Family = readonly [fund: string, asset: string, strategy: string, region: string];

const FAMILIES: readonly Family[] = [
  ['Contoso Equity Fund', 'Equity', 'Large Cap Core', 'US'],
  ['Contoso Equity Income Fund', 'Equity', 'Equity Income', 'US'],
  ['Contoso Equity Opportunities Fund', 'Equity', 'Multi-Cap Blend', 'US'],
  ['Contoso Growth Opportunities Fund', 'Equity', 'Large Cap Growth', 'US'],
  ['Contoso Small Cap Value Fund', 'Equity', 'Small Cap Value', 'US'],
  ['Contoso Small Cap Growth Fund', 'Equity', 'Small Cap Growth', 'US'],
  ['Contoso Mid Cap Growth Fund', 'Equity', 'Mid Cap Growth', 'US'],
  ['Contoso Mid Cap Value Fund', 'Equity', 'Mid Cap Value', 'US'],
  ['Contoso Large Cap Value Fund', 'Equity', 'Large Cap Value', 'US'],
  ['Contoso Dividend Growth Fund', 'Equity', 'Dividend Growth', 'US'],
  ['Contoso Quality Growth Fund', 'Equity', 'Quality Growth', 'US'],
  ['Contoso Low Volatility Equity Fund', 'Equity', 'Low Volatility', 'US'],
  ['Contoso Technology Innovation Fund', 'Equity', 'Sector: Technology', 'US'],
  ['Contoso Healthcare Leaders Fund', 'Equity', 'Sector: Health Care', 'US'],
  ['Contoso Financials Fund', 'Equity', 'Sector: Financials', 'US'],
  ['Contoso Energy Transition Fund', 'Equity', 'Sector: Energy', 'Global'],
  ['Contoso Real Estate Securities Fund', 'Equity', 'Real Estate Securities', 'US'],
  ['Contoso Infrastructure Fund', 'Equity', 'Listed Infrastructure', 'Global'],
  ['Contoso Sustainable Equity Fund', 'Equity', 'ESG Core', 'Global'],
  ['Contoso Global Equity Fund', 'Equity', 'Global Core', 'Global'],
  ['Contoso Global Dividend Fund', 'Equity', 'Global Dividend', 'Global'],
  ['Contoso International Opportunities Fund', 'Equity', 'International Growth', 'Non-US'],
  ['Contoso European Equity Fund', 'Equity', 'European Core', 'Europe'],
  ['Contoso Asia Pacific Equity Fund', 'Equity', 'Asia Pacific Core', 'Asia Pacific'],
  ['Contoso Japan Equity Fund', 'Equity', 'Japan Core', 'Japan'],
  ['Contoso Latin America Equity Fund', 'Equity', 'Latin America Core', 'Latin America'],
  ['Woodgrove Emerging Markets Equity Fund', 'Equity', 'Emerging Markets Value', 'Emerging Markets'],
  ['Woodgrove Frontier Markets Fund', 'Equity', 'Frontier Markets', 'Emerging Markets'],
  ['Woodgrove Global Smaller Companies Fund', 'Equity', 'Global Small Cap', 'Global'],
  ['Woodgrove International Value Fund', 'Equity', 'International Value', 'Non-US'],
  ['Contoso Core Plus Bond Fund', 'Fixed Income', 'Core Plus', 'US'],
  ['Contoso Short Duration Income Fund', 'Fixed Income', 'Short Duration', 'US'],
  ['Contoso Ultra-Short Bond Fund', 'Fixed Income', 'Ultra-Short', 'US'],
  ['Contoso Intermediate Government Fund', 'Fixed Income', 'Intermediate Government', 'US'],
  ['Contoso High Yield Fund', 'Fixed Income', 'High Yield', 'US'],
  ['Contoso Floating Rate Fund', 'Fixed Income', 'Bank Loans', 'US'],
  ['Contoso Inflation-Protected Bond Fund', 'Fixed Income', 'Inflation-Protected', 'US'],
  ['Contoso Municipal Income Fund', 'Fixed Income', 'National Municipal', 'US'],
  ['Contoso Tax-Free Intermediate Fund', 'Fixed Income', 'Intermediate Municipal', 'US'],
  ['Contoso Corporate Credit Fund', 'Fixed Income', 'Investment Grade Credit', 'US'],
  ['Woodgrove Global Fixed Income Fund', 'Fixed Income', 'Global Bond', 'Global'],
  ['Woodgrove Emerging Markets Debt Fund', 'Fixed Income', 'Emerging Markets Debt', 'Emerging Markets'],
  ['Woodgrove Global Total Return Fund', 'Fixed Income', 'Global Total Return', 'Global'],
  ['Contoso Strategic Multi-Asset Fund', 'Multi-Asset', 'Strategic Allocation', 'Global'],
  ['Contoso Balanced Allocation Fund', 'Multi-Asset', 'Moderate Allocation', 'US'],
  ['Contoso Conservative Allocation Fund', 'Multi-Asset', 'Conservative Allocation', 'US'],
  ['Contoso Growth Allocation Fund', 'Multi-Asset', 'Aggressive Allocation', 'US'],
  ['Contoso Target Date 2035 Fund', 'Multi-Asset', 'Target Date', 'US'],
  ['Contoso Target Date 2040 Fund', 'Multi-Asset', 'Target Date', 'US'],
  ['Contoso Target Date 2050 Fund', 'Multi-Asset', 'Target Date', 'US'],
  ['Contoso Real Assets Fund', 'Multi-Asset', 'Real Assets', 'Global'],
  ['Contoso Absolute Return Fund', 'Alternatives', 'Absolute Return', 'Global'],
  ['Contoso Market Neutral Fund', 'Alternatives', 'Equity Market Neutral', 'US'],
  ['Contoso Convertible Securities Fund', 'Alternatives', 'Convertibles', 'US'],
  ['Contoso Managed Futures Fund', 'Alternatives', 'Managed Futures', 'Global'],
  ['Contoso Liquid Alternatives Fund', 'Alternatives', 'Multi-Strategy', 'Global'],
];

export const SHARE_CLASSES = ['A', 'C', 'Advisor', 'R6', 'I', 'R'] as const;

export function classSuffix(shareClass: string): string {
  return shareClass === 'Advisor' ? 'Advisor Class' : `Class ${shareClass}`;
}

export const PRODUCTS: readonly MockProduct[] = FAMILIES.flatMap(([fund, asset, strategy, region], familyIndex) =>
  SHARE_CLASSES.map((shareClass, classIndex) => ({
    product_id: `SYN-${String(familyIndex * SHARE_CLASSES.length + classIndex + 1).padStart(6, '0')}`,
    family_id: `FAM-${String(familyIndex + 1).padStart(4, '0')}`,
    fund_name: fund,
    share_class: shareClass,
    display_name: `${fund} ${classSuffix(shareClass)}`,
    asset_class: asset,
    strategy,
    region,
    vehicle: 'Mutual Fund',
  })),
);

const BY_ID = new Map(PRODUCTS.map((product) => [product.product_id, product]));

export function productById(productId: string): MockProduct | undefined {
  return BY_ID.get(productId);
}

export function findProduct(fundName: string, shareClass: string): MockProduct {
  const product = PRODUCTS.find((item) => item.fund_name === fundName && item.share_class === shareClass);
  if (!product) throw new Error(`Mock catalogue has no ${fundName} / ${shareClass}`);
  return product;
}

export function toCandidate(product: MockProduct, score: number, matchedOn: string[]): ProductCandidate {
  return {
    product_id: product.product_id,
    display_name: product.display_name,
    fund_name: product.fund_name,
    share_class: product.share_class,
    score,
    matched_on: matchedOn,
  };
}

// ---- search ----------------------------------------------------------------------------------------
export function normalizeText(text: string): string {
  return text
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

const CLASS_WORDS: Record<string, string> = {
  a: 'A',
  c: 'C',
  advisor: 'Advisor',
  r6: 'R6',
  i: 'I',
  r: 'R',
  see: 'C',
  sea: 'C',
  clase: '',
};

const STOP_WORDS = new Set(['class', 'clase', 'shares', 'share', 'fund', 'the', 'fondo', 'de', 'acciones', 'of', 'a', 'an']);

function statedClass(tokens: string[]): string | null {
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index] ?? '';
    const next = tokens[index + 1] ?? '';
    const previous = tokens[index - 1] ?? '';
    if ((token === 'class' || token === 'clase') && CLASS_WORDS[next]) return CLASS_WORDS[next] ?? null;
    if ((next === 'shares' || next === 'class') && CLASS_WORDS[token]) return CLASS_WORDS[token] ?? null;
    if (token === 'advisor' || token === 'r6') return CLASS_WORDS[token] ?? null;
    if (previous === 'share' && CLASS_WORDS[token]) return CLASS_WORDS[token] ?? null;
  }
  return null;
}

export function searchCatalogue(query: string, top = 10): ProductSearchHit[] {
  const tokens = normalizeText(query).split(' ').filter(Boolean);
  if (tokens.length === 0) return [];
  const wanted = statedClass(tokens);
  const words = tokens.filter((token) => !STOP_WORDS.has(token) && !(wanted && CLASS_WORDS[token] === wanted && token.length <= 2));
  const scored: ProductSearchHit[] = [];

  for (const product of PRODUCTS) {
    const haystack = normalizeText(`${product.display_name} ${product.strategy} ${product.asset_class} ${product.region} ${product.product_id}`);
    const idMatch = normalizeText(product.product_id) === normalizeText(query);
    let matched = 0;
    for (const word of words) if (haystack.includes(word)) matched += 1;
    if (words.length > 0 && matched === 0 && !idMatch) continue;
    let score = words.length === 0 ? 0.3 : matched / words.length;
    if (wanted) score += product.share_class === wanted ? 0.18 : -0.22;
    if (idMatch) score = 1;
    score = Math.max(0, Math.min(1, score)) * 0.97;
    if (score < 0.2) continue;
    scored.push({
      product_id: product.product_id,
      display_name: product.display_name,
      fund_name: product.fund_name,
      share_class: product.share_class,
      asset_class: product.asset_class,
      score: Math.round(score * 1000) / 1000,
    });
  }

  scored.sort((left, right) => right.score - left.score || left.display_name.localeCompare(right.display_name));
  return scored.slice(0, Math.max(1, top));
}

export function catalogueStats(learnedAliases: number): CatalogueStats {
  const shareClasses: Record<string, number> = {};
  const assetClasses: Record<string, number> = {};
  for (const product of PRODUCTS) {
    shareClasses[product.share_class] = (shareClasses[product.share_class] ?? 0) + 1;
    assetClasses[product.asset_class] = (assetClasses[product.asset_class] ?? 0) + 1;
  }
  return {
    products: PRODUCTS.length,
    families: FAMILIES.length,
    share_classes: shareClasses,
    asset_classes: assetClasses,
    synthetic: true,
    note: "SYNTHETIC illustrative catalogue - not Contoso Woodgrove's product list.",
    learned_aliases: learnedAliases,
  };
}
