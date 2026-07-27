import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import hljs from "highlight.js";

/**
 * 共享 markdown 渲染器。
 * - 关闭原始 HTML（防注入），由 DOMPurify 兜底消毒
 * - 自动 linkify URL
 * - 代码块用 highlight.js 高亮
 * 长会话频繁调用，因此 md 实例全局复用。
 */

function escapeHtml(input: string): string {
  return input.replace(
    /[&<>"']/g,
    (ch) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[ch] as string,
  );
}

// 显式标注 md 类型，避免 highlight 回调内引用 escapeHtml 造成循环推断
const md: MarkdownIt = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>`;
      } catch {
        // 降级到普通转义
      }
    }
    return `<pre class="hljs"><code>${escapeHtml(str)}</code></pre>`;
  },
});

// 给所有链接加 target=_blank + rel=noopener，避免对话框被外部站点劫持。
// 复用默认规则的函数签名来推导参数类型，绕开 @types/markdown-it 命名导出限制。
const defaultLinkOpen = md.renderer.rules.link_open;
if (defaultLinkOpen) {
  md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
    const token = tokens[idx];
    const aIndex = token.attrIndex("target");
    if (aIndex < 0) {
      token.attrPush(["target", "_blank"]);
    } else {
      token.attrs![aIndex][1] = "_blank";
    }
    token.attrPush(["rel", "noopener noreferrer"]);
    return defaultLinkOpen(tokens, idx, options, env, self);
  };
}

export function renderMarkdown(src: string): string {
  const dirty = md.render(src ?? "");
  return DOMPurify.sanitize(dirty, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ["target", "rel"],
  });
}
