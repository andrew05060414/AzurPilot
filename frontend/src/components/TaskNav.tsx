import { useApp } from '../app/context'
import { usesLegacyLayout } from '../app/theme'
import { TaskNavFlyout } from './TaskNavFlyout'
import { TaskNavTree } from './TaskNavTree'

/**
 * 侧栏任务菜单模式：
 * 旧版主题强制使用树状手风琴；
 * 其余主题遵循 taskNavMode 偏好（默认为向下展开的树状手风琴 'tree'，可切换为向右浮出的二级菜单 'flyout'）。
 */
export function TaskNav({ defaultOpenKey }: { defaultOpenKey?: string } = {}) {
  const { theme, taskNavMode } = useApp()
  return usesLegacyLayout(theme) || taskNavMode === 'tree'
    ? <TaskNavTree defaultOpenKey={defaultOpenKey}/>
    : <TaskNavFlyout defaultOpenKey={defaultOpenKey}/>
}
