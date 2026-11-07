import { posix } from 'node:path'
import { fileURLToPath } from 'node:url'
import { slug } from 'github-slugger'
import { defineConfig, type DefaultTheme } from 'vitepress'

const repository = 'https://github.com/BigDawnGhost/wenyi'

const sidebar: DefaultTheme.SidebarItem[] = [
  {
    text: 'Getting started',
    collapsed: false,
    items: [
      { text: 'Overview', link: '/guide/' },
      { text: 'Desktop', link: '/guide/desktop' },
      { text: 'Web deployment', link: '/guide/web' },
      { text: 'CLI', link: '/guide/cli' },
    ],
  },
  {
    text: 'Translation & review',
    collapsed: false,
    items: [
      { text: 'Workspace', link: '/guide/workspace' },
      { text: 'Translation pipeline', link: '/guide/pipeline' },
    ],
  },
  {
    text: 'Reference',
    collapsed: false,
    items: [
      { text: 'Configuration', link: '/reference/configuration' },
      { text: 'Formats and exports', link: '/reference/formats' },
    ],
  },
  {
    text: 'Development',
    collapsed: true,
    items: [
      { text: 'Overview', link: '/development/' },
      { text: 'Architecture', link: '/development/architecture' },
      { text: 'Pipeline contracts', link: '/development/pipeline' },
      { text: 'Web development', link: '/development/web' },
      { text: 'Desktop development', link: '/development/desktop' },
      { text: 'Frontend conventions', link: '/development/frontend' },
    ],
  },
  {
    text: 'Releases & contributing',
    collapsed: true,
    items: [
      { text: 'Desktop releases', link: '/development/releasing' },
      { text: 'Python package publishing', link: '/development/publishing' },
      { text: 'Contributing', link: `${repository}/blob/main/CONTRIBUTING.md` },
    ],
  },
]

const zhSidebar: DefaultTheme.SidebarItem[] = [
  {
    text: '入门',
    collapsed: false,
    items: [
      { text: '概览', link: '/zh/guide/' },
      { text: 'Desktop', link: '/zh/guide/desktop' },
      { text: 'Web 部署', link: '/zh/guide/web' },
      { text: 'CLI', link: '/zh/guide/cli' },
    ],
  },
  {
    text: '翻译与校阅',
    collapsed: false,
    items: [
      { text: '工作台', link: '/zh/guide/workspace' },
      { text: '翻译流程', link: '/zh/guide/pipeline' },
    ],
  },
  {
    text: '参考',
    collapsed: false,
    items: [
      { text: '配置项', link: '/zh/reference/configuration' },
      { text: '格式与导出', link: '/zh/reference/formats' },
    ],
  },
  {
    text: '开发维护',
    collapsed: true,
    items: [
      { text: '开发入口', link: '/zh/development/' },
      { text: '架构与模块边界', link: '/zh/development/architecture' },
      { text: '流程开发契约', link: '/zh/development/pipeline' },
      { text: 'Web 开发', link: '/zh/development/web' },
      { text: 'Desktop 开发', link: '/zh/development/desktop' },
      { text: '前端规范', link: '/zh/development/frontend' },
    ],
  },
  {
    text: '发布与贡献',
    collapsed: true,
    items: [
      { text: '桌面发布', link: '/zh/development/releasing' },
      { text: 'Python 包发布', link: '/zh/development/publishing' },
      { text: '贡献指南', link: `${repository}/blob/main/docs/zh/CONTRIBUTING.md` },
    ],
  },
]

export default defineConfig({
  title: 'Wenyi',
  description: 'Bringing literature into your language.',
  base: '/wenyi/',
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
      themeConfig: {
        sidebar,
      },
    },
    zh: {
      label: '简体中文',
      lang: 'zh-Hans',
      title: '文译',
      description: '将被语言阻隔的作品，带到读者的语言中。',
      themeConfig: {
        sidebar: zhSidebar,
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
