/**
 * PlayLaunch Studio — Stage 4 Node.js Vendor Bridge (stages/bridges/aso_node_bridge.cjs)
 *
 * Orchestrates UNMODIFIED vendors/google-play-scraper (ESM) + vendors/aso (CJS via
 * store dependency injection) with gentle rate-limit delays, per-call timeouts, and
 * graceful degradation on partial failures.
 *
 * Usage:
 *   node stages/bridges/aso_node_bridge.cjs <input_request.json> <output_result.json>
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function withTimeout(promise, ms, label) {
  let timer = null;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Timeout after ${ms}ms (${label})`)), ms);
  });
  return Promise.race([promise, timeoutPromise]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

async function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  if (!inputPath || !outputPath) {
    console.error('Usage: node aso_node_bridge.cjs <input.json> <output.json>');
    process.exit(2);
  }

  const req = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
  const studioRoot = path.resolve(__dirname, '..', '..');
  const gplayEntry = path.join(studioRoot, 'vendors', 'google-play-scraper', 'index.js');
  const asoEntry = path.join(studioRoot, 'vendors', 'aso', 'index.js');
  const asoCalcEntry = path.join(studioRoot, 'vendors', 'aso', 'lib', 'calc.js');

  const gplayMod = await import(pathToFileURL(gplayEntry).href);
  const gplay = gplayMod.default || gplayMod;
  const asoFactory = require(asoEntry);
  const calc = require(asoCalcEntry);

  const country = req.country || 'eg';
  const lang = req.lang || 'ar';
  const delayMs = Number.isFinite(req.delay_ms) ? req.delay_ms : 350;
  const callTimeoutMs = Number.isFinite(req.call_timeout_ms) ? req.call_timeout_ms : 15000;
  const maxCompetitors = Number.isFinite(req.max_competitors) ? req.max_competitors : 10;
  const maxScoredKeywords = Number.isFinite(req.max_scored_keywords) ? req.max_scored_keywords : 6;
  const seedKeywords = Array.isArray(req.seed_keywords) ? req.seed_keywords.filter(Boolean) : [];
  const ownPackage = String(req.own_package_name || '').toLowerCase();

  const warnings = [];

  // Build injected modern store adapter for unmodified vendors/aso
  const defaults = { country, lang, throttle: 5 };
  const wrapped = (method) => async (opts) => {
    if (delayMs > 0) await sleep(Math.min(delayMs, 300));
    return withTimeout(
      gplay[method](Object.assign({}, defaults, opts)),
      callTimeoutMs,
      `gplay.${method}`
    );
  };

  const MAX_KEYWORD_LENGTH = 25;
  async function getSuggestLength(keyword, length = 1) {
    if (length > Math.min(keyword.length, MAX_KEYWORD_LENGTH)) {
      return { length: undefined, index: undefined };
    }
    const suggestions = await wrapped('suggest')({ term: keyword.slice(0, length) });
    const index = Array.isArray(suggestions) ? suggestions.indexOf(keyword) : -1;
    if (index === -1) return getSuggestLength(keyword, length + 1);
    return { length, index };
  }

  const getCollection = (app) => (app.free ? gplay.collection.TOP_FREE : gplay.collection.TOP_PAID);
  const getGenre = (app) => app.genreId;
  const modernGplayStore = {
    MAX_SEARCH: 60,
    MAX_LIST: 60,
    list: wrapped('list'),
    search: wrapped('search'),
    app: wrapped('app'),
    similar: wrapped('similar'),
    suggest: wrapped('suggest'),
    getInstallsScore: (apps) => {
      const avg = apps.reduce((s, a) => s + (a.minInstalls || 0), 0) / Math.max(apps.length, 1);
      return { avg, score: calc.zScore(1000000, avg) };
    },
    getSuggestScore: async (keyword) => {
      try {
        const stats = await getSuggestLength(keyword);
        const score = !stats.length
          ? 1
          : calc.aggregate([10, 1], [calc.iScore(1, MAX_KEYWORD_LENGTH, stats.length), calc.izScore(4, stats.index)]);
        return Object.assign({}, stats, { score });
      } catch (_err) {
        return { length: undefined, index: undefined, score: 1 };
      }
    },
    getCollection,
    getGenre,
    getCollectionQuery: (app) => ({ collection: getCollection(app), category: getGenre(app), num: 60 }),
  };

  const asoClient = asoFactory(modernGplayStore);

  // 1. Autocomplete suggestions per seed keyword
  const autocompleteMap = {};
  const discoveredKeywords = new Set(seedKeywords);
  for (const kw of seedKeywords) {
    try {
      const suggestions = await withTimeout(
        gplay.suggest({ term: kw, lang, country }),
        callTimeoutMs,
        `suggest:${kw}`
      );
      autocompleteMap[kw] = Array.isArray(suggestions) ? suggestions : [];
      for (const s of autocompleteMap[kw].slice(0, 4)) {
        if (typeof s === 'string' && s.trim()) discoveredKeywords.add(s.trim());
      }
    } catch (err) {
      warnings.push(`suggest failed for '${kw}': ${err.message}`);
      autocompleteMap[kw] = [];
    }
    if (delayMs > 0) await sleep(delayMs);
  }

  // 2. Search top competitors across top seed keywords
  const competitorMap = new Map();
  const searchTerms = seedKeywords.slice(0, 4);
  for (const term of searchTerms) {
    try {
      const results = await withTimeout(
        gplay.search({ term, num: Math.min(maxCompetitors, 10), lang, country }),
        callTimeoutMs,
        `search:${term}`
      );
      for (const item of Array.isArray(results) ? results : []) {
        if (!item || !item.appId) continue;
        if (ownPackage && item.appId.toLowerCase() === ownPackage) continue;
        if (!competitorMap.has(item.appId)) {
          competitorMap.set(item.appId, {
            app_id: item.appId,
            title: item.title || '',
            developer: item.developer || '',
            summary: item.summary || '',
            score: item.score || 0,
            icon: item.icon || '',
            matched_term: term,
          });
        }
      }
    } catch (err) {
      warnings.push(`search failed for '${term}': ${err.message}`);
    }
    if (delayMs > 0) await sleep(delayMs);
  }

  // 3. Enrich top N competitors with full Play Store metadata (graceful degradation per app)
  const competitors = [];
  const selectedIds = Array.from(competitorMap.keys()).slice(0, maxCompetitors);
  for (const appId of selectedIds) {
    const base = competitorMap.get(appId);
    try {
      const full = await withTimeout(
        gplay.app({ appId, lang, country }),
        callTimeoutMs,
        `app:${appId}`
      );
      competitors.push({
        app_id: appId,
        title: full.title || base.title,
        developer: full.developer || base.developer,
        summary: full.summary || base.summary,
        description: (full.description || '').slice(0, 4000),
        score: full.score || base.score || 0,
        ratings: full.ratings || 0,
        reviews_count: full.reviews || 0,
        installs: full.installs || '',
        min_installs: full.minInstalls || 0,
        genre: full.genre || '',
        genre_id: full.genreId || '',
        free: full.free !== false,
        ad_supported: Boolean(full.adSupported),
        updated: full.updated || null,
        icon: full.icon || base.icon,
        matched_term: base.matched_term,
        enriched: true,
      });
    } catch (err) {
      warnings.push(`app detail fetch failed for '${appId}' (kept basic search record): ${err.message}`);
      competitors.push(Object.assign({}, base, { description: '', enriched: false }));
    }
    if (delayMs > 0) await sleep(delayMs);
  }

  // 4. Score keyword opportunity matrix via unmodified vendors/aso with injected modern store
  const keywordMatrix = [];
  const keywordsToScore = Array.from(discoveredKeywords).slice(0, maxScoredKeywords);
  for (const kw of keywordsToScore) {
    try {
      const scoreObj = await withTimeout(
        asoClient.scores(kw),
        Math.max(callTimeoutMs * 2, 25000),
        `aso.scores:${kw}`
      );
      const difficulty = Number(scoreObj && scoreObj.difficulty ? scoreObj.difficulty.score : 5);
      const traffic = Number(scoreObj && scoreObj.traffic ? scoreObj.traffic.score : 1);
      // Higher traffic + lower difficulty = stronger opportunity (0..10 scale)
      const opportunity = Number(((traffic * 0.65) + ((10 - difficulty) * 0.35)).toFixed(2));
      keywordMatrix.push({
        keyword: kw,
        locale: `${lang}-${country.toUpperCase()}`,
        traffic: Number(traffic.toFixed(2)),
        difficulty: Number(difficulty.toFixed(2)),
        opportunity_score: opportunity,
        autocomplete_count: (autocompleteMap[kw] || []).length,
        status: 'ok',
      });
    } catch (err) {
      warnings.push(`aso.scores degraded for '${kw}': ${err.message}`);
      keywordMatrix.push({
        keyword: kw,
        locale: `${lang}-${country.toUpperCase()}`,
        traffic: null,
        difficulty: null,
        opportunity_score: null,
        autocomplete_count: (autocompleteMap[kw] || []).length,
        status: 'degraded',
        error: err.message,
      });
    }
    if (delayMs > 0) await sleep(delayMs);
  }

  keywordMatrix.sort((a, b) => (b.opportunity_score ?? -1) - (a.opportunity_score ?? -1));

  const output = {
    node_version: process.version,
    country,
    lang,
    seed_keywords: seedKeywords,
    autocomplete_suggestions: autocompleteMap,
    competitors,
    keyword_opportunity_matrix: keywordMatrix,
    warnings,
  };

  fs.writeFileSync(outputPath, JSON.stringify(output, null, 2), 'utf8');
}

main().catch((err) => {
  console.error('ASO_NODE_BRIDGE_FATAL:', err && err.stack ? err.stack : err);
  process.exit(1);
});
