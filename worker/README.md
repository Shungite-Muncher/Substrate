# Substrate API worker

A Cloudflare Worker serving `/api/chat` and `/api/part` for the hosted dashboard and the Chrome extension. The free tier (100k requests/day) is far more than a demo needs, and your API keys stay server-side.

```bash
cd worker
npx wrangler login
npx wrangler secret put GEMINI_API_KEY        # free key from https://aistudio.google.com/apikey
npx wrangler secret put ACCESS_CODE           # any passphrase; share it with demo users
npx wrangler secret put NEXAR_CLIENT_ID       # optional: Octopart via https://portal.nexar.com
npx wrangler secret put NEXAR_CLIENT_SECRET   # optional
npx wrangler secret put MOUSER_API_KEY        # optional free fallback
npx wrangler deploy
```

Then put the printed `https://substrate-api.<you>.workers.dev` URL in `site/config.js` (or in the dashboard's **API settings**) and in the extension's options.

Parts in the BOM are answered from the published snapshot and cost nothing. Other parts try Octopart first, within `NEXAR_MONTHLY_PART_BUDGET`, then Mouser. Every result is edge-cached for 3 days.
