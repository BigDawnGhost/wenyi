import assert from 'node:assert/strict'
import { access, readFile } from 'node:fs/promises'
import { join, posix } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createMarkdownRenderer, resolveConfig } from 'vitepress'

const root = fileURLToPath(new URL('..', import.meta.url))
const config = await resolveConfig(root, 'build')
const markdown = await createMarkdownRenderer(config.srcDir, config.markdown, config.site.base)

assert.ok(
  config.pages.includes('index.md') && config.pages.includes('zh/index.md'),
  'Both documentation homepages must be included in the rendering checks',
)

function sidebarLinks(items) {
  return items.flatMap((item) => [
    ...(item.link ? [item.link] : []),
    ...sidebarLinks(item.items ?? []),
  ])
}

test('the favicon uses the Wenyi emblem under the GitHub Pages base path', async () => {
  const icons = config.site.head.filter(([tag, attributes]) => (
    tag === 'link' && attributes.rel === 'icon'
  ))
  assert.equal(icons.length, 1)
  const [, attributes] = icons[0]
  assert.equal(attributes.type, 'image/png')
  assert.equal(attributes.href, `${config.site.base}wenyi-emblem.png`)
  await access(join(config.vite.publicDir, 'wenyi-emblem.png'))
})

for (const [locale, prefix] of [['root', ''], ['zh', 'zh/']]) {
  test(`${locale}: the homepage has only documentation and repository actions`, async () => {
    const page = `${prefix}index.md`
    const env = { relativePath: page }
    markdown.render(await readFile(join(config.srcDir, page), 'utf8'), env)
    assert.deepEqual(
      env.frontmatter.hero.actions.map((action) => action.link),
      [`/${prefix}guide/`, 'https://github.com/BigDawnGhost/wenyi'],
    )
    assert.equal(env.frontmatter.hero.image.alt, '', 'The background emblem is decorative')
    assert.equal(env.frontmatter.features, undefined)
    assert.equal(env.content.trim(), '')
  })

  test(`${locale}: the sidebar reaches every topic without header navigation`, () => {
    const theme = { ...config.site.themeConfig, ...config.site.locales[locale].themeConfig }
    assert.deepEqual(theme.nav ?? [], [])
    assert.deepEqual(theme.socialLinks ?? [], [])
    assert.ok(Array.isArray(theme.sidebar), 'Use one grouped sidebar across documentation sections')
    const groups = locale === 'zh'
      ? ['入门', '翻译与校阅', '参考', '开发维护', '发布与贡献']
      : ['Getting started', 'Translation & review', 'Reference', 'Development', 'Releases & contributing']
    assert.deepEqual(theme.sidebar.map((group) => group.text), groups)
    const links = new Set(sidebarLinks(theme.sidebar))
    const pages = config.pages.filter(
      (page) => page.startsWith('zh/') === (locale === 'zh') && page !== `${prefix}index.md`,
    )
    assert.ok(pages.length > 0)
    for (const page of pages) {
      const route = `/${page.replace(/(^|\/)index\.md$/, '$1').replace(/\.md$/, '')}`
      assert.ok(links.has(route), `${page} must be reachable from the sidebar`)
    }
  })
}

for (const page of config.pages) {
  test(`${page}: prose renders cleanly and leaves language switching to the site`, async () => {
    const source = await readFile(join(config.srcDir, page), 'utf8')
    const frontmatter = source.match(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/)?.[0] ?? ''
    const offset = frontmatter.split('\n').length - 1
    const tokens = markdown.parse(source.slice(frontmatter.length), { relativePath: page })
    const failures = []

    for (const token of tokens) {
      if (token.type !== 'inline') continue
      for (const child of token.children ?? []) {
        // Literal Markdown examples belong in code, not unparsed prose.
        if (child.type === 'text' && /\*\*|__/.test(child.content)) {
          failures.push(`Line ${(token.map?.[0] ?? 0) + offset + 1}: ${child.content}`)
        }
        if (child.type === 'link_open') {
          const href = child.attrGet('href') ?? ''
          if (!href || /^(?:[a-z][a-z\d+.-]*:|[/#])/i.test(href)) continue
          const target = posix.join(posix.dirname(page), href.split(/[?#]/, 1)[0])
          if (config.pages.includes(target) && target.startsWith('zh/') !== page.startsWith('zh/')) {
            failures.push(`Use the site's language switch instead of a prose link to ${target}`)
          }
          try {
            await access(join(config.srcDir, target))
            const lineRange = href.split('#')[1]?.match(/^L(\d+)(?:C\d+)?(?:-L(\d+)(?:C\d+)?)?$/)
            if (lineRange) {
              const linkedSource = await readFile(join(config.srcDir, target), 'utf8')
              const lineCount = linkedSource.split('\n').length - Number(linkedSource.endsWith('\n'))
              const first = Number(lineRange[1])
              const last = Number(lineRange[2] ?? lineRange[1])
              if (first < 1 || last < first || last > lineCount) {
                failures.push(`Source line outside file: ${href}`)
              }
            }
          } catch {
            failures.push(`Missing relative link: ${href}`)
          }
        }
      }
    }

    assert.deepEqual(failures, [], 'Check emphasis, relative links and in-document language links')
  })
}
