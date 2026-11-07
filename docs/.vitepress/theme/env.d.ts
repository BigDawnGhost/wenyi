declare module '*.vue' {
  import type { Theme } from 'vitepress'

  const component: NonNullable<Theme['Layout']>
  export default component
}
