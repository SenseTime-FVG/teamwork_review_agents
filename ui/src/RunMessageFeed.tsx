import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { RunLog } from "./types";
import { presentRunLogs } from "./runLogPresentation";

function messageTime(value: number): string {
  return new Date(value * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function MarkdownMessage(props: { children: string }) {
  return (
    <div className="run-message-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, ...linkProps }) => <a {...linkProps} target="_blank" rel="noreferrer">{children}</a>,
        }}
      >
        {props.children}
      </ReactMarkdown>
    </div>
  );
}

export function RunMessageFeed(props: { logs: RunLog[] }) {
  const messages = useMemo(() => presentRunLogs(props.logs), [props.logs]);
  const viewportRef = useRef<HTMLDivElement>(null);
  const previousLogCountRef = useRef(0);
  const [following, setFollowing] = useState(true);
  const [hasNewMessages, setHasNewMessages] = useState(false);

  function scrollToLatest(behavior: ScrollBehavior = "smooth") {
    const viewport = viewportRef.current;
    if (!viewport) return;
    viewport.scrollTo({ top: viewport.scrollHeight, behavior });
    setFollowing(true);
    setHasNewMessages(false);
  }

  useEffect(() => {
    if (props.logs.length === previousLogCountRef.current) return;
    previousLogCountRef.current = props.logs.length;
    if (following) {
      window.requestAnimationFrame(() => scrollToLatest("auto"));
    } else {
      setHasNewMessages(true);
    }
  }, [props.logs.length, following]);

  return (
    <div className="run-message-feed">
      <div
        ref={viewportRef}
        className="run-message-viewport"
        onScroll={() => {
          const viewport = viewportRef.current;
          if (!viewport) return;
          const nearBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
          setFollowing(nearBottom);
          if (nearBottom) setHasNewMessages(false);
        }}
      >
        {messages.map((message) => (
          <article key={message.id} className={`run-message message-${message.kind}`}>
            <div className="run-message-rail"><span /></div>
            <div className="run-message-card">
              <header>
                <strong>{message.title}</strong>
                <span className="run-message-meta">
                  {message.repeatCount > 1 && <em>重复 {message.repeatCount} 次</em>}
                  <time>{messageTime(message.lastCreatedAt)}</time>
                </span>
              </header>
              {message.body && (
                message.kind === "agent"
                  ? <MarkdownMessage>{message.body}</MarkdownMessage>
                  : <pre className="run-message-body">{message.body}</pre>
              )}
              {message.detail && <pre className="run-message-detail">{message.detail}</pre>}
              <details className="run-message-raw">
                <summary>查看原始数据</summary>
                <pre>{message.raw}</pre>
              </details>
            </div>
          </article>
        ))}
        {messages.length === 0 && <div className="run-message-empty">等待 Agent 输出消息…</div>}
      </div>
      {hasNewMessages && (
        <button className="run-message-new" onClick={() => scrollToLatest()}>
          有新消息，回到底部
        </button>
      )}
    </div>
  );
}
