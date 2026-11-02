import "dotenv/config";

// 監視専用構成ではログイン用のAPP_ID/APP_SECRETを必要としない。
export const env = {
  isProduction: process.env.NODE_ENV === "production",
};
