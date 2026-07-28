const CHANGE_LABELS: Record<string, string> = {
  dependency_contract: "Dependency rules changed",
  python_compatibility: "Python support changed",
  platform_installability: "Installation support changed",
  release_availability: "Release available again",
  release_withdrawal: "Release withdrawn",
  security_advisory: "Security advisory",
  packaging_metadata: "Package metadata changed",
  runtime_behavior_unobservable: "Runtime impact needs review",
  unknown: "Unclassified package change",
};

export function formatChangeType(value: string) {
  return (
    CHANGE_LABELS[value] ??
    value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
}
