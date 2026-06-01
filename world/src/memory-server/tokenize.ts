const STOPWORDS = new Set<string>([
  // latin
  'the', 'an', 'of', 'to', 'in', 'on', 'and', 'or', 'is', 'are', 'be', 'for', 'with', 'at', 'by', 'it', 'this', 'that',
  // 中文常见虚词（单字）
  '的', '了', '和', '是', '在', '我', '你', '他', '她', '它', '也', '都', '就', '与', '及', '等', '吧', '呢', '啊', '吗',
])

const TOKEN_RE = /[a-z0-9]+|[一-鿿]/g

export function tokenize(text: string): string[] {
  const lowered = text.toLowerCase()
  const out: string[] = []
  for (const m of lowered.matchAll(TOKEN_RE)) {
    const tok = m[0]
    if (STOPWORDS.has(tok)) continue
    out.push(tok)
  }
  return out
}
