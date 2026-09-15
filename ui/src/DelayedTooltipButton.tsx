import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { fitOverlayToViewport } from "./overlayPlacement";

export const TOOLTIP_DELAY_MS = 2000;

// 延迟说明不占据按钮布局；移开、失焦、点击或按 Escape 时立即收起。
export function DelayedTooltipButton(props: {
  children: ReactNode;
  className: string;
  description: string;
  onClick: () => void;
}) {
  const tooltipId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [visible, setVisible] = useState(false);

  function clearTimer() {
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = null;
  }

  function hide() {
    clearTimer();
    setVisible(false);
  }

  function schedule() {
    clearTimer();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      setVisible(true);
    }, TOOLTIP_DELAY_MS);
  }

  useEffect(() => clearTimer, []);
  useLayoutEffect(() => {
    if (!visible) return;
    const updatePlacement = () => {
      const button = buttonRef.current;
      const tooltip = tooltipRef.current;
      if (!button || !tooltip) return;
      const bounds = button.getBoundingClientRect();
      const viewport = document.documentElement;
      const placement = fitOverlayToViewport(bounds.left, 280, viewport.clientWidth);
      tooltip.style.width = `${placement.width}px`;
      tooltip.style.left = `${placement.left}px`;
      const below = bounds.bottom + 8;
      tooltip.style.top = `${Math.max(8, below + tooltip.offsetHeight <= viewport.clientHeight - 8 ? below : bounds.top - tooltip.offsetHeight - 8)}px`;
    };
    updatePlacement();
    window.addEventListener("resize", updatePlacement);
    window.addEventListener("scroll", updatePlacement, true);
    return () => {
      window.removeEventListener("resize", updatePlacement);
      window.removeEventListener("scroll", updatePlacement, true);
    };
  }, [visible]);

  return <>
    <button
      ref={buttonRef}
      type="button"
      className={props.className}
      aria-describedby={visible ? tooltipId : undefined}
      onMouseEnter={schedule}
      onMouseLeave={hide}
      onFocus={schedule}
      onBlur={hide}
      onKeyDown={(event) => { if (event.key === "Escape") hide(); }}
      onClick={() => { hide(); props.onClick(); }}
    >{props.children}</button>
    {visible && createPortal(
      <div ref={tooltipRef} id={tooltipId} className="delayed-button-tooltip" role="tooltip">{props.description}</div>,
      document.body,
    )}
  </>;
}
