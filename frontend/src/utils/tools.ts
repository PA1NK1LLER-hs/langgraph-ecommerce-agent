/** 工具名 → 功能标签映射。
 *
 * 后端工具为动态注册（内置 + MCP），按名称关键字归类展示标签：
 * 搜索 / 代码 / RPA / 记忆。匹配顺序有意为之：RPA 先于搜索，
 * 避免 'browser' 被 'browse' 抢先归类为搜索。
 */
export type ToolTagKey = 'search' | 'code' | 'rpa' | 'memory' | 'specialist'

export interface ToolTag {
  key: ToolTagKey
  label: string
}

export function tagForTool(name: string): ToolTag | null {
  const n = name.toLowerCase()
  if (/(rpa|automation|playwright|selenium|screenshot|clicker|ui_|browser)/.test(n)) {
    return { key: 'rpa', label: 'RPA' }
  }
  if (/(code|exec|python|shell|bash|sql|jupyter|terminal|run_)/.test(n)) {
    return { key: 'code', label: '代码' }
  }
  if (/(search|query|web|browse|fetch|lookup|retriev|knowledge)/.test(n)) {
    return { key: 'search', label: '搜索' }
  }
  if (/(memory|memories|recall)/.test(n)) {
    return { key: 'memory', label: '记忆' }
  }
  if (/(specialist|researcher|coder|analyst)/.test(n)) {
    return { key: 'specialist', label: '子代理' }
  }
  return null
}
