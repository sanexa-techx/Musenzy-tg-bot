import { bigint, integer, pgTable, serial, text, timestamp, uniqueIndex } from "drizzle-orm/pg-core";

export const favoriteTracksTable = pgTable(
  "favorite_tracks",
  {
    id: serial("id").primaryKey(),
    userId: bigint("user_id", { mode: "number" }).notNull(),
    videoId: text("video_id").notNull(),
    url: text("url").notNull(),
    title: text("title").notNull(),
    duration: integer("duration").notNull().default(0),
    thumbnail: text("thumbnail"),
    createdAt: timestamp("created_at", { withTimezone: true }).defaultNow().notNull(),
  },
  (table) => [
    uniqueIndex("favorite_tracks_user_video_idx").on(table.userId, table.videoId),
  ],
);

export type FavoriteTrack = typeof favoriteTracksTable.$inferSelect;