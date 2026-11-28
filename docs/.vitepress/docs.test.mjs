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

  test(`${locale}: each topic belongs to one header tab and its sidebar`, () => {
    const { nav, sidebar } = config.site.locales[locale].themeConfig
    const sections = ['guide', 'development', 'reference'].map((name) => `/${prefix}${name}/`)
    const routes = config.pages
      .filter((page) => page.startsWith('zh/') === (locale === 'zh') && page !== `${prefix}index.md`)
      .map((page) => `/${page.replace(/\/index\.md$/, '/').replace(/\.md$/, '')}`)
    assert.deepEqual(nav.map(({ text }) => text), locale === 'zh'
      ? ['入门', '开发', '参考']
      : ['Guide', 'Development', 'Reference'])

    for (const [i, section] of sections.entries()) {
      const links = sidebarLinks(sidebar[section]).filter((link) => link.startsWith('/'))
      assert.deepEqual(links.sort(), routes.filter((route) => route.startsWith(section)).sort())
      assert.ok(links.includes(nav[i].link), `${nav[i].text} must open a page in its own sidebar`)
    }
    for (const route of routes) {
      const active = nav.filter(({ activeMatch }) => new RegExp(activeMatch).test(route))
      const section = sections.find((path) => route.startsWith(path))
      assert.ok(section, `No section for ${route}`)
      assert.deepEqual(active, [nav[sections.indexOf(section)]], `Wrong active tab for ${route}`)
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
