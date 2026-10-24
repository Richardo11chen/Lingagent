/**
 * unified diff 的渲染：一行一层，按前缀染色。
 *
 * 只做染色的原因：diff 是后端用 difflib 算好的（工具写文件时顺手留的），
 * 前端不需要理解它，只要别把 `+/-` 当成 markdown 列表。
 */
export default function DiffBlock({ diff }: { diff: string }) {
  return (
    <div className="diff">
      {diff.split('\n').map((line, i) => {
        const cls =
          line.startsWith('+++') || line.startsWith('---')
            ? 'diff-file'
            : line.startsWith('@@')
              ? 'diff-hunk'
              : line.startsWith('+')
                ? 'diff-add'
                : line.startsWith('-')
                  ? 'diff-del'
                  : 'diff-ctx'
        return (
          <div key={i} className={cls}>
            {line || ' '}
          </div>
        )
      })}
    </div>
  )
}
