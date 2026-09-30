import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownText({ children, inline = false }: { children: string; inline?: boolean }) {
  if (inline) {
    return (
      <span className="markdown-inline">
        <ReactMarkdown
          skipHtml
          remarkPlugins={[remarkGfm]}
          allowedElements={["p", "em", "strong", "code", "a", "del", "br"]}
          unwrapDisallowed
          components={{ p: ({ children }) => <span>{children}</span> }}
        >
          {children}
        </ReactMarkdown>
      </span>
    );
  }
  return (
    <div className="markdown-body">
      <ReactMarkdown skipHtml remarkPlugins={[remarkGfm]}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
