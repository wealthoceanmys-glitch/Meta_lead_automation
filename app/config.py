from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    database_url: str = Field(default="sqlite:///./local_crm.db", alias="DATABASE_URL")

    jwt_secret: str = Field(default="dev-secret-change-me", alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"

    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="admin123", alias="ADMIN_PASSWORD")
    frontend_origin: str = Field(default="http://localhost:3000", alias="FRONTEND_ORIGIN")

    meta_verify_token: str = Field(default="verify-token", alias="META_VERIFY_TOKEN")

    # Keep both names supported
    meta_access_token: str = Field(default="", alias="META_ACCESS_TOKEN")
    meta_page_access_token: str = Field(default="", alias="META_PAGE_ACCESS_TOKEN")
    meta_page_id: str = Field(default="", alias="META_PAGE_ID")

    whatsapp_enabled: bool = Field(default=False, alias="WHATSAPP_ENABLED")
    whatsapp_access_token: str = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_business_account_id: str = Field(default="", alias="WHATSAPP_BUSINESS_ACCOUNT_ID")

    # Meta App ID — required for the Resumable Upload API (/{app_id}/uploads) used
    # when submitting a template with an image/video/document header sample.
    # Find it in the Meta App Dashboard (developers.facebook.com/apps → your app → App ID).
    meta_app_id: str = Field(default="", alias="META_APP_ID")

    # Old/default template env, kept for backward compatibility
    whatsapp_template_name: str = Field(
        default="woi_seminar_registration_followup",
        alias="WHATSAPP_TEMPLATE_NAME",
    )

    # New separate template envs
    whatsapp_registration_template_name: str = Field(
        default="woi_seminar_registration_followup",
        alias="WHATSAPP_REGISTRATION_TEMPLATE_NAME",
    )

    whatsapp_invitation_template_name: str = Field(
        default="woi_free_seminar_invite",
        alias="WHATSAPP_INVITATION_TEMPLATE_NAME",
    )

    whatsapp_language_code: str = Field(default="en", alias="WHATSAPP_LANGUAGE_CODE")

    seminar_timezone: str = Field(default="Asia/Kolkata", alias="SEMINAR_TIMEZONE")
    seminar_venue: str = Field(
        default="Kuvempunagara, Mysuru - https://g.co/kgs/FDbcqh",
        alias="SEMINAR_VENUE",
    )

    seminar_thursday_time: str = Field(
        default="6:00 PM to 8:00 PM",
        alias="SEMINAR_THURSDAY_TIME",
    )
    seminar_thursday_arrival: str = Field(
        default="5:45 PM",
        alias="SEMINAR_THURSDAY_ARRIVAL",
    )

    seminar_sunday_time: str = Field(
        default="10:30 AM to 12:30 PM",
        alias="SEMINAR_SUNDAY_TIME",
    )
    seminar_sunday_arrival: str = Field(
        default="10:15 AM",
        alias="SEMINAR_SUNDAY_ARRIVAL",
    )

    keep_alive_enabled: bool = Field(default=True, alias="KEEP_ALIVE_ENABLED")
    render_external_url: str = Field(default="", alias="RENDER_EXTERNAL_URL")

    class Config:
        env_file = ".env"
        populate_by_name = True


settings = Settings()
