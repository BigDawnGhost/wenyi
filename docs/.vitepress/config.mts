import { posix } from 'node:path'
import { fileURLToPath } from 'node:url'
import { slug } from 'github-slugger'
import { defineConfig, type DefaultTheme } from 'vitepress'

const repository = 'https://github.com/BigDawnGhost/wenyi'
const base = '/wenyi/'

function navigation(locale: 'en' | 'zh') {
  const zh = locale === 'zh'
  const prefix = zh ? '/zh' : ''
  const guide = `${prefix}/guide/`
  const development = `${prefix}/development/`
  const reference = `${prefix}/reference/`

  return {
    nav: [
      { text: zh ? '入门' : 'Guide', link: guide, activeMatch: `^${guide}` },
      { text: zh ? '开发' : 'Development', link: development, activeMatch: `^${development}` },
      { text: zh ? '参考' : 'Reference', link: `${reference}configuration`, activeMatch: `^${reference}` },
    ],
    sidebar: {
      [guide]: [
        {
          text: zh ? '入门' : 'Getting started',
          items: [
            { text: zh ? '概览' : 'Overview', link: guide },
            { text: 'Desktop', link: `${guide}desktop` },
            { text: zh ? 'Web 部署' : 'Web deployment', link: `${guide}web` },
            { text: 'CLI', link: `${guide}cli` },
          ],
        },
        {
          text: zh ? '翻译与校阅' : 'Translation & review',
          items: [
            { text: zh ? '工作台' : 'Workspace', link: `${guide}workspace` },
            { text: zh ? '翻译流程' : 'Translation pipeline', link: `${guide}pipeline` },
          ],
        },
      ],
      [development]: [
        {
          text: zh ? '架构与契约' : 'Architecture & contracts',
          items: [
            { text: zh ? '开发入口' : 'Overview', link: development },
            { text: zh ? '架构与模块边界' : 'Architecture', link: `${development}architecture` },
            { text: zh ? '流程开发契约' : 'Pipeline contracts', link: `${development}pipeline` },
          ],
        },
        {
          text: zh ? '平台与界面' : 'Platforms & UI',
          items: [
            { text: zh ? 'Web 开发' : 'Web development', link: `${development}web` },
            { text: zh ? 'Desktop 开发' : 'Desktop development', link: `${development}desktop` },
            { text: zh ? '前端规范' : 'Frontend conventions', link: `${development}frontend` },
            { text: zh ? '设计令牌' : 'Design tokens', link: `${development}design-tokens` },
          ],
        },
        {
          text: zh ? '发布与贡献' : 'Releases & contributing',
          items: [
            { text: zh ? '桌面发布' : 'Desktop releases', link: `${development}releasing` },
            { text: zh ? 'Python 包发布' : 'Python package publishing', link: `${development}publishing` },
            {
              text: zh ? '贡献指南' : 'Contributing',
              link: `${repository}/blob/main/${zh ? 'docs/zh/' : ''}CONTRIBUTING.md`,
            },
          ],
        },
      ],
      [reference]: [
        {
          text: zh ? '参考' : 'Reference',
          items: [
            { text: zh ? '配置项' : 'Configuration', link: `${reference}configuration` },
            { text: zh ? '格式与导出' : 'Formats and exports', link: `${reference}formats` },
          ],
        },
      ],
    },
  } satisfies Pick<DefaultTheme.Config, 'nav' | 'sidebar'>
}

export default defineConfig({
  title: 'Wenyi',
  description: 'Bringing literature into your language.',
  base,
  head: [
    ['link', { rel: 'icon', type: 'image/png', href: `${base}wenyi-emblem.png` }],
  ],
  // Application development URLs are examples, not documentation pages.
  ignoreDeadLinks: [/^https?:\/\/localhost(?::\d+)?(?:\/|$)/],
  srcExclude: ['**/README.md', '**/CONTRIBUTING.md'],
  vite: {
    publicDir: fileURLToPath(new URL('../images', import.meta.url)),
  },
  markdown: {
    // Preserve the GitHub heading anchors used by the existing guides.
    anchor: { slugify: slug },
    config(md) {
      const renderLink = md.renderer.rules.link_open!
      md.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
        const token = tokens[index]
        const href = token.attrGet('href')
        if (href && !/^(?:[a-z][a-z\d+.-]*:|[/#])/i.test(href)) {
          const pathname = href.split(/[?#]/, 1)[0]
          const target = posix.join(posix.dirname(env.relativePath), pathname)
          // Keep repository-only links working without changing the Markdown sources.
          if (
            target.startsWith('../') ||
            /(^|\/)(README|CONTRIBUTING)\.md$/.test(target)
          ) {
            token.attrSet(
              'href',
              `${repository}/blob/main/${posix.join('docs', target)}${href.slice(pathname.length)}`,
            )
          }
        }
        return renderLink(tokens, index, options, env, renderer)
      }
    },
  },
  themeConfig: {
    logo: '/wenyi-emblem.png',
    socialLinks: [{ icon: 'github', link: repository, ariaLabel: 'Wenyi · GitHub' }],
    search: {
      provider: 'local',
      options: {
        miniSearch: {
          options: {
            // Use the same word boundaries when indexing and searching Chinese or English.
            tokenize: (text) =>
              Array.from(new Intl.Segmenter('zh', { granularity: 'word' }).segment(text))
                .filter((part) => part.isWordLike)
                .map((part) => part.segment),
          },
        },
        locales: {
          zh: {
            translations: {
              button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
              modal: {
                noResultsText: '没有找到相关结果',
                resetButtonTitle: '清除搜索',
                footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' },
              },
            },
          },
        },
      },
    },
  },
  locales: {
    root: {
      label: 'English',
      lang: 'en',
      themeConfig: navigation('en'),
    },
    zh: {
      label: '简体中文',
      lang: 'zh-Hans',
      title: '文译',
      description: '将被语言阻隔的作品，带到读者的语言中。',
      themeConfig: {
        ...navigation('zh'),
        outline: { label: '本页目录' },
        docFooter: { prev: '上一页', next: '下一页' },
        sidebarMenuLabel: '目录',
        returnToTopLabel: '回到顶部',
        darkModeSwitchLabel: '外观',
        langMenuLabel: '切换语言',
      },
    },
  },
})
