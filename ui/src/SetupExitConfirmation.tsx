import { useLayoutEffect, useRef } from "react";

type Props = {
  onContinue: () => void;
  onDiscard: () => void;
};

// 使用独立的顶层模态框，背景向导保持原步骤与草稿，不能被 Tab 或点击操作。
export function SetupExitConfirmation({ onContinue, onDiscard }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const continueButton = useRef<HTMLButtonElement>(null);

  useLayoutEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    element?.showModal();
    continueButton.current?.focus();
    return () => {
      element?.close();
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
        previousFocus.focus({ preventScroll: true });
      }
    };
  }, []);

  return <dialog
    ref={dialog}
    className="quick-setup-exit"
    role="alertdialog"
    aria-modal="true"
    aria-labelledby="quick-setup-exit-title"
    aria-describedby="quick-setup-exit-description"
    onKeyDown={(event) => {
      if (event.key !== "Tab") return;
      // 显式循环按钮焦点，避免浏览器把末尾 Tab 移到地址栏。
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      if (index < 0 || (event.shiftKey ? index === 0 : index === buttons.length - 1)) {
        event.preventDefault();
        buttons[event.shiftKey ? buttons.length - 1 : 0]?.focus();
      }
    }}
    onCancel={(event) => {
      event.preventDefault();
      event.stopPropagation();
      onContinue();
    }}
  >
    <div className="quick-setup-exit-heading">
      <span className="quick-setup-exit-icon" aria-hidden="true">!</span>
      <div>
        <h2 id="quick-setup-exit-title">退出一键配置？</h2>
        <p id="quick-setup-exit-description">本次未保存的配置将被丢弃。</p>
      </div>
    </div>
    <footer className="quick-setup-exit-actions">
      <button ref={continueButton} type="button" className="button primary" onClick={onContinue}>继续配置</button>
      <button type="button" className="button danger" onClick={onDiscard}>放弃并退出</button>
    </footer>
  </dialog>;
}
