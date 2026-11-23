/**
 * PlayLaunch Studio — Node v24 Compatibility Smoke Test
 * Verifies unmodified vendors/google-play-scraper (ESM) + vendors/aso (CJS via store injection).
 */
'use strict';

const path = require('path');
const { pathToFileURL } = require('url');

async function main() {
  const root = path.resolve(__dirname, '..');
  const gplayEntry = path.join(root, 'vendors', 'google-play-scraper', 'index.js');
  const asoEntry = path.join(root, 'vendors', 'aso', 'index.js');
  const asoCalcEntry = path.join(root, 'vendors', 'aso', 'lib', 'calc.js');

  console.log(`Node version: ${process.version}`);

  // 1. Load unmodified ESM google-play-scraper
  const gplayMod = await import(pathToFileURL(gplayEntry).href);
  const gplay = gplayMod.default || gplayMod;

  const suggest = await gplay.suggest({ term: 'offline music', lang: 'en', country: 'us' });
  if (!Array.isArray(suggest) || suggest.length === 0) {
    throw new Error('gplay.suggest returned empty result');
  }
  console.log(`1) google-play-scraper.suggest: OK (${suggest.length} suggestions, e.g. ${JSON.stringify(suggest.slice(0, 3))})`);

  const search = await gplay.search({ term: 'offline music player', num: 3, lang: 'en', country: 'us' });
  if (!Array.isArray(search) || search.length === 0) {
    throw new Error('gplay.search returned empty result');
  }
  console.log(`2) google-play-scraper.search:  OK (${search.length} results, top1=${search[0].appId})`);

  // 2. Load unmodified CJS aso with modern google-play-scraper injected as store adapter
  //    (Avoids aso's bundled outdated google-play-scraper@8.x without touching vendor code.)
  const asoFactory = require(asoEntry);
  const calc = require(asoCalcEntry);
  const defaults = { country: 'us', lang: 'en', throttle: 10 };
  const wrapped = (method) => (opts) => gplay[method](Object.assign({}, defaults, opts));
  const MAX_KEYWORD_LENGTH = 25;

  async function getSuggestLength(keyword, length = 1) {
    if (length > Math.min(keyword.length, MAX_KEYWORD_LENGTH)) {
      return { length: undefined, index: undefined };
    }
    const suggestions = await wrapped('suggest')({ term: keyword.slice(0, length) });
    const index = suggestions.indexOf(keyword);
    if (index === -1) return getSuggestLength(keyword, length + 1);
    return { length, index };
  }

  const getCollection = (app) => (app.free ? gplay.collection.TOP_FREE : gplay.collection.TOP_PAID);
  const getGenre = (app) => app.genreId;
  const modernGplayStore = {
    MAX_SEARCH: 250,
    MAX_LIST: 120,
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
      const stats = await getSuggestLength(keyword);
      const score = !stats.length
        ? 1
        : calc.aggregate([10, 1], [calc.iScore(1, MAX_KEYWORD_LENGTH, stats.length), calc.izScore(4, stats.index)]);
      return Object.assign({}, stats, { score });
    },
    getCollection,
    getGenre,
    getCollectionQuery: (app) => ({ collection: getCollection(app), category: getGenre(app), num: 120 }),
  };

  const client = asoFactory(modernGplayStore);
  const scores = await client.scores('offline music');
  if (!scores || !scores.difficulty || !scores.traffic) {
    throw new Error('aso.scores did not return difficulty and traffic objects');
  }
  console.log(
    `3) aso.scores (injected store): OK (difficulty=${scores.difficulty.score}, traffic=${scores.traffic.score})`
  );
  console.log('ALL NODE V24 VENDOR SMOKE TESTS PASSED.');
}

main().catch((err) => {
  console.error('NODE VENDOR SMOKE TEST FAILED:', err && err.stack ? err.stack : err);
  process.exit(1);
});
