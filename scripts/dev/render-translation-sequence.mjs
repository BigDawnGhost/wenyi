// Regenerate both standalone README images: node scripts/dev/render-translation-sequence.mjs
import { writeFileSync } from 'node:fs'

const pipeline = 0, model = 1, glossary = 2, store = 3, review = 4, autofix = 5
const lanes = [86, 305, 510, 689, 871, 1056]
const names = [
  ['Pipeline', '翻译流水线'], ['Model', '语言模型'], ['Glossary', '术语库'],
  ['Project store', '项目存储'], ['Review', '全书审校'], ['Autofix', '自动修复'],
]

// Participants are logical roles; a message can group multiple model calls or storage reads.
function sequence({ call, reply, group, otherwise }) {
  call(pipeline, model, 'Analyze the book', '译前分析原书')
  reply(model, pipeline, 'Style and book context', '文风指导与全书背景')
  call(pipeline, store, 'Save preparation results', '保存章节梗概、文风与全书概览')

  group('loop', pipeline, store, 'batches in source order', '按原书顺序逐批处理', () => {
    call(pipeline, store, 'Read batch progress and recent translations', '读取本批进度与近期译文')
    reply(store, pipeline, 'Saved segments and extraction checkpoint', '已存段落与术语抽取检查点')
    group('opt', pipeline, store, 'translation pending', '本批尚未翻译', () => {
      call(pipeline, glossary, 'Read all current mappings', '读取当前全量术语映射')
      group('alt', pipeline, model, 'standard translation', '标准翻译', () => {
        call(pipeline, model, 'Translate in context', '结合前文与术语翻译')
        reply(model, pipeline, 'Translated segments', '逐段译文')
        group('opt', pipeline, model, 'polishing enabled', '启用润色', () => {
          call(pipeline, model, 'Polish the draft', '润色本批译文')
          reply(model, pipeline, 'Polished segments', '润色后的段落')
        })
        otherwise('three-draft synthesis', '三稿精翻')
        call(pipeline, model, 'Draft 3 times in parallel', '冻结上下文，并行生成三稿')
        reply(model, pipeline, 'Three independent drafts', '三份独立译稿')
        call(pipeline, model, 'Synthesize with source', '对照原文综合三稿')
        reply(model, pipeline, 'Synthesized translation', '综合译文')
      })
      call(pipeline, store, 'Validate segment count, then save targets', '校验段落数量，保存本批译文')
    })
    group('opt', pipeline, store, 'terms not yet extracted', '本批术语尚未提取', () => {
      call(pipeline, model, 'Extract from aligned pairs', '从已存原译对抽取术语')
      reply(model, pipeline, 'Terms and translations', '术语及其译名')
      call(pipeline, glossary, 'Add mappings; record conflicting names', '录入新词，登记译名冲突')
      call(pipeline, store, 'Checkpoint term extraction', '记录本批术语检查点')
    })
  })

  group('opt', pipeline, autofix, 'all chapters translated and Review enabled', '全书译完且启用审校', () => {
    call(pipeline, review, 'Start whole-book review', '启动全书审校')
    call(review, glossary, 'Read the final glossary snapshot', '读取最终术语表快照')
    call(review, store, 'Read aligned text', '读取原文与正式译文')
    group('loop', model, review, 'review rounds within budget', '在轮次预算内复查', () => {
      call(review, model, 'Check passages against the final glossary', '对照最终术语表逐段核查')
      group('opt', model, review, 'more evidence needed', '需要补充证据', () => {
        call(model, review, 'Request supporting passages', '请求相关段落作为证据')
        reply(review, model, 'Source and target excerpts', '返回原文与译文片段')
      })
      reply(model, review, 'Candidate issues and supporting evidence', '候选问题与段落证据')
      call(review, model, 'Arbitrate conflicting recommendations', '裁决互相冲突的修订建议')
      group('opt', model, review, 'shadow revisions enabled', '启用副本试改', () => {
        call(review, model, 'Draft fixes against the round snapshot', '基于本轮快照拟订修订')
        reply(model, review, 'Patches for the shadow copy only', '仅用于审校副本的补丁')
      })
    })
    reply(review, pipeline, 'Review report and proposed revisions', '审校报告与候选修订')
    group('opt', pipeline, autofix, 'Autofix enabled and revisions ready', '启用自动修复且有待发布修订', () => {
      call(pipeline, autofix, 'Publish reviewed changes', '发布审校修订')
      call(autofix, store, 'Journal patches and expected targets', '先记录发布索引与预期译文')
      group('alt', store, autofix, 'saved text matches', '正式译文仍匹配', () => {
        call(autofix, store, 'Apply verified replacements', '写回通过校验的段落修订')
        otherwise('conflicting edit', '与已有编辑冲突')
        call(autofix, store, 'Record conflict; preserve edited text', '记录冲突，保留已有编辑')
      })
    })
  })
}

function render(zh) {
  const lang = zh ? 'zh-CN' : 'en'
  const text = (en, cn) => (zh ? cn : en).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  const title = text('Whole-book translation sequence', '全书翻译时序')
  const desc = text(
    'Batches reuse context and glossary mappings. Translation and term extraction have separate checkpoints. Review gathers evidence and revises a shadow copy; Autofix journals and verifies changes before publishing.',
    '翻译逐批复用上下文和术语，译文与抽词分别记录进度。审校取证后只试改副本；自动修复先登记发布索引，校验正式译文后才写回。',
  )
  const width = 1142
  const frames = [], arrows = [], labels = [], open = []
  let y = 71

  function message(kind, from, to, en, cn) {
    const a = lanes[from], b = lanes[to]
    labels.push(`    <text class="message" x="${(a + b) / 2}" y="${y}">${text(en, cn)}</text>`)
    arrows.push(`  <path class="${kind}" d="M${a} ${y + 7}H${b}"/>`)
    y += 29
  }

  function group(type, first, last, en, cn, draw) {
    const x = lanes[first] - 68 + open.length * 7
    const right = lanes[last] + 68 - open.length * 7
    const top = y - 9
    frames.push(`  <path class="tab" d="M${x} ${top}h53v12l-8 8h-45z"/>`)
    labels.push(`    <text class="keyword" x="${x + 7}" y="${top + 15}">${type}</text>`,
      `    <text class="guard" x="${x + 63}" y="${top + 15}">[${text(en, cn)}]</text>`)
    open.push({ x, right })
    y += 27
    draw()
    open.pop()
    frames.push(`  <rect class="frame" x="${x}" y="${top}" width="${right - x}" height="${y - top - 10}"/>`)
    y += 10
  }

  function otherwise(en, cn) {
    const { x, right } = open.at(-1)
    frames.push(`  <path class="divider" d="M${x} ${y - 11}H${right}"/>`)
    labels.push(`    <text class="guard" x="${x + 10}" y="${y + 6}">[${text(en, cn)}]</text>`)
    y += 28
  }

  sequence({
    call: (...args) => message('call', ...args), reply: (...args) => message('reply', ...args),
    group, otherwise,
  })
  const foot = y + 1
  const height = foot + 46
  function actors(top) {
    return lanes.map((x, i) => `  <rect class="actor" x="${x - 68}" y="${top}" width="136" height="32" rx="2"/>
  <text class="name" x="${x}" y="${top + 22}">${text(...names[i])}</text>`).join('\n')
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" lang="${lang}" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="title desc">
  <!-- Generated by scripts/dev/render-translation-sequence.mjs. -->
  <title id="title">${title}</title>
  <desc id="desc">${desc}</desc>
  <defs>
    <marker id="solid" viewBox="0 0 8 8" refX="7.5" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto"><path d="m0 0 8 4-8 4z" fill="currentColor"/></marker>
    <marker id="open" viewBox="0 0 8 8" refX="7.5" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto"><path d="m1 1 6 3-6 3" fill="none" stroke="currentColor" stroke-width="1.3"/></marker>
    <g id="labels">
${labels.join('\n')}
    </g>
    <!-- Leave transparent gaps where a lifeline or frame passes behind text. -->
    <mask id="label-gaps" maskUnits="userSpaceOnUse" x="0" y="0" width="${width}" height="${height}">
      <rect width="100%" height="100%" fill="white"/>
      <use href="#labels" fill="black" stroke="black" stroke-width="4" stroke-linejoin="round"/>
    </mask>
  </defs>
  <style>
    :root { color: #222 }
    @media (prefers-color-scheme: dark) { :root { color: #eee } }
    text { font-family: system-ui, "Noto Sans CJK SC", sans-serif }
    .name, .message { text-anchor: middle; font-size: 16px }
    .name { font-weight: 600; fill: currentColor }
    .guard { font-size: 15px }
    .keyword { font: bold 13px monospace }
    .actor, .tab, .life, .frame, .divider { fill: none; stroke: currentColor }
    .actor, .tab { stroke-opacity: .7 }
    .life, .frame, .divider { stroke-opacity: .5 }
    .divider { stroke-dasharray: 5 4 }
    .call { stroke: currentColor; stroke-width: 1.25; marker-end: url(#solid) }
    .reply { stroke: currentColor; stroke-dasharray: 4 4; stroke-width: 1.2; marker-end: url(#open) }
  </style>
  <g mask="url(#label-gaps)">
${lanes.map(x => `  <path class="life" d="M${x} 46V${foot}"/>`).join('\n')}
${frames.join('\n')}
  </g>
${arrows.join('\n')}
  <use href="#labels" fill="currentColor"/>
${actors(14)}
${actors(foot)}
</svg>
`
}

for (const zh of [false, true]) {
  const name = `translation-sequence${zh ? '-zh' : ''}.svg`
  writeFileSync(new URL(`../../docs/images/${name}`, import.meta.url), render(zh))
}
