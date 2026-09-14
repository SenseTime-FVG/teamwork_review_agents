import { useRef, useState } from "react";
import type { InputHTMLAttributes } from "react";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "readOnly"> & {
  value: string;
  onValueChange: (value: string) => void;
};

// 首次操作前阻止浏览器预填，操作后保留原生输入和已保存凭据候选。
export function SetupIntentInput({ value, onValueChange, ...props }: Props) {
  const interacted = useRef(false);
  const [editable, setEditable] = useState(false);

  function activate(input: HTMLInputElement, trusted: boolean) {
    if (!trusted) return;
    interacted.current = true;
    // 在浏览器处理当前按键或焦点之前解锁，避免丢失第一次输入或粘贴。
    input.readOnly = false;
    setEditable(true);
  }

  return <input
    {...props}
    value={value}
    readOnly={!editable}
    spellCheck={false}
    autoCapitalize="none"
    onPointerDown={(event) => activate(event.currentTarget, event.nativeEvent.isTrusted)}
    onKeyDown={(event) => {
      if (!["Tab", "Escape", "Shift", "Control", "Alt", "Meta"].includes(event.key)) {
        activate(event.currentTarget, event.nativeEvent.isTrusted);
      }
    }}
    onChange={(event) => {
      if (interacted.current) onValueChange(event.currentTarget.value);
      else event.currentTarget.value = value;
    }}
    onAnimationStart={(event) => {
      // 某些浏览器预填只改变 DOM；此处恢复草稿，不轮询清空用户输入。
      if (event.animationName === "quick-setup-autofill" && !interacted.current) {
        event.currentTarget.value = value;
      }
    }}
  />;
}
