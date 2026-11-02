import { z } from "zod";
import { createRouter, publicQuery } from "./middleware";
import { getDashboardSnapshot } from "./dashboard";

export const dashboardRouter = createRouter({
  snapshot: publicQuery
    .input(z.object({ symbol: z.string().regex(/^[A-Z0-9]+_JPY$/).optional() }).optional())
    .query(({ input }) => getDashboardSnapshot(input?.symbol)),
});
