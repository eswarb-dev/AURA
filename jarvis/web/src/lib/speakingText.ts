export function splitSpeakingText(text: string): string[] {
  const clean = text
    .replace(/<cosmo:[^>]*>[\s\S]*?<\/cosmo:[^>]*>/g, ' ')
    .replace(/<jarvis:[^>]*>[\s\S]*?<\/jarvis:[^>]*>/g, ' ')
    .replace(/[#*`_>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  if (!clean) return []

  const paragraphSegments = clean.split(/\n\s*\n/).map((s) => s.trim()).filter((s) => s.length > 0)
  if (paragraphSegments.length > 1) return paragraphSegments

  const sentenceSegments = clean.match(/[^.!?]*[.!?]+(?=\s|$)/g) || [clean]
  return sentenceSegments.map((s) => s.trim()).filter((s) => s.length > 0)
}
