// SC7 (specs/workbench-retrieve-product-seam.md): the product route
// table as pure data, so a test can assert the IA without a DOM.
// ``App.tsx`` destructures this same constant into its ``<Route>``
// table, so the two cannot drift; its catch-all renders a 404 for any
// other path. ``/kb`` and ``/monitor`` were retired — they are NOT
// product routes.

/** The product route patterns, mirroring the ``<Route>`` table in
 * ``App.tsx`` (``:thread_id`` is the React Router param token). */
export const PRODUCT_ROUTE_PATHS = ["/", "/t/:thread_id"] as const;
