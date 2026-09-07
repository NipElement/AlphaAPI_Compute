// Arco forwards scrollbar attrs to its actual scrolling container. Make long
// option lists reachable by Tab as well as the select's arrow-key navigation.
export const selectScrollbar = {
  type: "embed" as const,
  outerClass: "",
  outerStyle: {},
  tabindex: 0,
};
