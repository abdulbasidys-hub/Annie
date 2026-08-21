/**
 * Screenshot harness.
 *
 * Renders each route at desktop and phone widths and reports any console
 * errors, failed requests or page exceptions. Used to check the interface by
 * looking at it rather than by assuming the JSX was right.
 *
 *   node tools/shoot.js [outputDir]
 */

import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

// By name, not by IP — the dev server binds `localhost`, which on Windows is
// ::1 only. See vite.config.js at the repo root.
const BASE = process.env.APP_URL || 'http://localhost:5180'
const OUT = process.argv[2] || './shots'

const ROUTES = [
  ['dashboard', '/'],
  ['annie', '/annie'],
  ['trends', '/trends'],
  ['trend-detail', '/trends/ai-narrative-100k'],
  ['trend-thin', '/trends/brand-parody-1m'],
  ['tokens', '/tokens'],
  ['token-detail', '/tokens/NEURxx000000Zk4Qv9Lm2Rt8Wp3Nc7Hb0'],
  ['launchpads', '/launchpads'],
  ['launchpad-detail', '/launchpads/unknown-9fk2mq1a'],
  ['creators', '/creators'],
  ['creator-detail', '/creators/Cr0tRw9Km4Pz7Vn2Lb8Qs5Xd3Fj6Hg1Ay0'],
  ['narratives', '/narratives'],
  ['research', '/research'],
  ['reports', '/reports'],
  ['sources', '/sources'],
  ['health', '/health'],
  ['settings', '/settings'],
]

const VIEWPORTS = [
  ['desktop', { width: 1440, height: 1000 }],
  ['mobile', { width: 390, height: 844 }],
]

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const problems = []

for (const [vpName, viewport] of VIEWPORTS) {
  for (const theme of vpName === 'desktop' ? ['dark', 'light'] : ['dark']) {
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
    const page = await context.newPage()

    page.on('console', (msg) => {
      if (msg.type() === 'error') problems.push(`[console] ${vpName}/${theme} ${page.url()} :: ${msg.text()}`)
    })
    page.on('pageerror', (err) => problems.push(`[pageerror] ${vpName}/${theme} ${page.url()} :: ${err.message}`))
    page.on('requestfailed', (req) => {
      if (!req.url().includes('favicon')) {
        problems.push(`[request] ${vpName}/${theme} ${req.url()} :: ${req.failure()?.errorText}`)
      }
    })

    for (const [name, path] of ROUTES) {
      await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle' })
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(400)

      // An empty chat proves nothing about how a reply renders — the claim
      // badge, citations and tool trace are the parts worth checking.
      if (name === 'annie') {
        await page.fill('.chat__input', 'What changed today?')
        await page.click('.btn--annie')
        await page.waitForTimeout(700)
        await page.fill('.chat__input', 'What separates $1M+ tokens from $100k ones?')
        await page.click('.btn--annie')
        await page.waitForTimeout(900)
      }

      // Horizontal overflow is the single most common mobile failure and is
      // invisible in a screenshot that was captured at full page width.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
      )
      if (overflow) {
        const w = await page.evaluate(() => document.documentElement.scrollWidth)
        problems.push(`[overflow] ${vpName}/${theme} ${path} :: body scrolls horizontally (${w}px)`)
      }

      await page.screenshot({
        path: `${OUT}/${vpName}-${theme}-${name}.png`,
        fullPage: vpName === 'desktop',
      })
    }

    await context.close()
  }
}

await browser.close()

if (problems.length) {
  console.log(`\n${problems.length} PROBLEM(S):\n`)
  for (const p of [...new Set(problems)]) console.log('  ' + p)
  process.exitCode = 1
} else {
  console.log('\nNo console errors, page exceptions, failed requests, or horizontal overflow.')
}
console.log(`\nScreenshots written to ${OUT}`)
