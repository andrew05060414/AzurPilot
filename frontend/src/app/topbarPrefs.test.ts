import { beforeEach, describe, expect, it, vi } from 'vitest'

// 模块在导入时就会往 document 上落一次档位，所以桩必须早于导入建立。
const stubs = vi.hoisted(() => {
  const values = new Map<string, string>()
  const dataset: Record<string, string> = {}
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  })
  vi.stubGlobal('document', {documentElement: {dataset}})
  return {values, dataset}
})

const {readTabSize, setTabSize, TAB_SIZES, initialPagePath} = await import('./topbarPrefs')

beforeEach(() => {
  stubs.values.clear()
})

describe('创建路由前恢复上次页面', () => {
  it.each(['', '#', '#/'])('全新打开根地址 %s 时直接选择记忆页面', hash => {
    stubs.values.set('azurpilot.last-path', '/i/demo-main/overview')
    expect(initialPagePath('navigate', hash)).toBe('/i/demo-main/overview')
  })

  it.each(['reload', 'back_forward', undefined] as const)('导航类型 %s 保留当前地址', type => {
    expect(initialPagePath(type, '#/', '/i/demo-main/overview')).toBeUndefined()
  })

  it('显式深链接不被上次页面覆盖', () => {
    expect(initialPagePath('navigate', '#/settings', '/i/demo-main/overview')).toBeUndefined()
  })

  it.each(['', '//example.com', 'https://example.com'])('忽略无效记忆 %s', path => {
    expect(initialPagePath('navigate', '#/', path)).toBeUndefined()
  })

  it('记忆为首页时保留首页', () => {
    expect(initialPagePath('navigate', '#/', '/')).toBe('/')
  })
})

describe('实例标签栏大小', () => {
  it('保存四个档位并应用到 :root', () => {
    expect(TAB_SIZES).toEqual(['sm', 'md', 'lg', 'xl'])
    for (const size of TAB_SIZES) {
      setTabSize(size)
      expect(readTabSize()).toBe(size)
      expect(stubs.dataset.tabSize).toBe(size)
    }
  })

  it('没存过档位时落在第三档', () => {
    expect(readTabSize()).toBe('lg')
  })
})
