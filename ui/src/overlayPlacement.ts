// 弹层优先与触发器左侧对齐，必要时向左移动并缩窄，始终保留屏幕边距。
export function fitOverlayToViewport(anchorLeft: number, desiredWidth: number, viewportWidth: number) {
  const margin = Math.min(8, Math.max(0, viewportWidth / 2));
  const width = Math.min(Math.max(0, desiredWidth), Math.max(0, viewportWidth - margin * 2));
  return {
    width,
    left: Math.min(Math.max(margin, anchorLeft), Math.max(margin, viewportWidth - margin - width)),
  };
}
