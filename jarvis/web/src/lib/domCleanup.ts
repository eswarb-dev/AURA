/** Only for imperative nodes created by this application, never React children. */
export function safeRemoveOwnedNode(node: Node | null | undefined, owner: Node): void {
  if (!node || node.parentNode !== owner) return
  try {
    owner.removeChild(node)
  } catch (error) {
    // A third-party mutation may have detached the node during removal.
    if (!(error instanceof DOMException && error.name === 'NotFoundError')) throw error
  }
}

export function isRemoveChildNotFound(error: Error): boolean {
  return error.name === 'NotFoundError' && error.message.includes('removeChild')
}
