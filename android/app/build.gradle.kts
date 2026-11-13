plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.nudgy.android"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.nudgy.android"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    // Test-only: JUnit, and org.json (built into Android, but not into plain JVM unit tests).
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
