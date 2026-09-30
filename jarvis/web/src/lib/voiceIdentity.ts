export type VoiceFeatures = {
  rms: number
  zeroCrossingRate: number
  durationMs: number
}

export function extractVoiceFeatures(samples: Float32Array[], sampleRate: number): VoiceFeatures {
  const values = samples.flatMap((sample) => Array.from(sample))
  if (!values.length) return { rms: 0, zeroCrossingRate: 0, durationMs: 0 }
  const rms = Math.sqrt(values.reduce((sum, value) => sum + value * value, 0) / values.length)
  let crossings = 0
  for (let i = 1; i < values.length; i++) {
    if ((values[i - 1] < 0 && values[i] >= 0) || (values[i - 1] >= 0 && values[i] < 0)) crossings += 1
  }
  return { rms, zeroCrossingRate: crossings / values.length, durationMs: Math.round((values.length / sampleRate) * 1000) }
}

export function voiceFeaturesToEmbedding(features: VoiceFeatures): number[] {
  return [features.rms, features.zeroCrossingRate, features.durationMs / 10000]
}
