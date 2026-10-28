/** Asset and liability vocabulary for the finance views. Pure data. */

export const ASSET_TYPES = [
  { value: 'property', label: 'Property', subtypes: ['home', 'buy to let', 'land'] },
  { value: 'investment', label: 'Investment', subtypes: ['isa', 'lisa', 'junior isa', 'general investment account', 'shares', 'funds', 'crypto'] },
  { value: 'pension', label: 'Pension', subtypes: ['workplace', 'private', 'sipp', 'junior sipp'] },
  { value: 'vehicle', label: 'Vehicle', subtypes: ['car', 'motorbike', 'van'] },
  { value: 'other', label: 'Other', subtypes: ['valuables', 'money owed to me'] },
];

export const LIABILITY_TYPES = [
  { value: 'mortgage', label: 'Mortgage', subtypes: ['repayment', 'interest only'] },
  { value: 'loan', label: 'Loan', subtypes: ['personal loan', 'car finance pcp', 'car finance hp'] },
  { value: 'student_loan', label: 'Student loan', subtypes: ['plan 1', 'plan 2', 'plan 4', 'plan 5', 'postgraduate'] },
  { value: 'bnpl', label: 'Buy now, pay later', subtypes: ['klarna', 'clearpay', 'paypal pay in 3'] },
  { value: 'other', label: 'Other debt', subtypes: ['money owed to a person', 'overdraft', 'store card'] },
];

export function typesFor(kind) {
  return kind === 'asset' ? ASSET_TYPES : LIABILITY_TYPES;
}

export function typeLabel(kind, value) {
  return typesFor(kind).find((t) => t.value === value)?.label || value;
}

/** Investments and pensions track contributions and gain/loss. */
export const CONTRIBUTION_TYPES = new Set(['investment', 'pension']);
