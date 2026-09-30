export type GenderEstimate = 'male' | 'female' | 'unknown'
export type IdentityAddress = 'Sir' | "Ma'am" | 'Mam' | 'Mx' | 'Guest'
export type AuraAddress = IdentityAddress | ''

export type AddressInput = {
  isOwner: boolean
  ownerAddress?: IdentityAddress | string
  genderEstimate: GenderEstimate
  genderConfidence: number
  threshold?: number
}

function normalizeAddress(value?: string): AuraAddress {
  if (value === 'Mam') return "Ma'am"
  if (value === 'Sir' || value === "Ma'am" || value === 'Mx' || value === 'Guest') return value
  return value ? value as AuraAddress : ''
}

export function getAddressFromIdentityAndEstimate(input: AddressInput): { address: IdentityAddress | string; reason: 'owner_profile' | 'explicit_profile' | 'low_confidence_neutral' } {
  if (input.isOwner) return { address: normalizeAddress(input.ownerAddress) || 'Sir', reason: 'owner_profile' }
  return { address: 'Guest', reason: 'low_confidence_neutral' }
}

export function getEffectiveAddress(input: {
  ownerVerified: boolean
  ownerAddress?: string
  sessionAddressOverride?: AuraAddress
  currentIdentityAddress?: string
  currentIdentity?: string
  matchedProfile?: { preferred_address?: string; display_name?: string; id?: string } | null
  preferredAddress?: string | null
  liveIdentityAddressAllowed?: boolean
  ownerProfileEnrolled?: boolean
  visualEstimateAddress?: string
  visualEstimateConfidence?: number
  threshold?: number
}): { address: AuraAddress; reason: 'owner_verified' | 'session_override' | 'identity' | 'explicit_profile' | 'default' | 'neutral' } {
  const ownerAddress = normalizeAddress(input.ownerAddress) || 'Sir'
  const matchedAddress = normalizeAddress(input.matchedProfile?.preferred_address || input.preferredAddress || undefined)
  if (input.liveIdentityAddressAllowed && matchedAddress && matchedAddress !== 'Guest') return { address: matchedAddress, reason: 'explicit_profile' }
  if (input.liveIdentityAddressAllowed && input.ownerVerified) return { address: ownerAddress, reason: 'owner_verified' }
  if (input.sessionAddressOverride) return { address: input.sessionAddressOverride, reason: 'session_override' }
  const identityAddress = normalizeAddress(input.currentIdentityAddress)
  if (input.liveIdentityAddressAllowed && input.currentIdentity && input.currentIdentity !== 'guest' && identityAddress && identityAddress !== 'Guest') {
    return { address: identityAddress, reason: 'identity' }
  }
  if (input.ownerProfileEnrolled) return { address: 'Sir', reason: 'default' }
  return { address: 'Guest', reason: 'neutral' }
}
