import Foundation
import Security

enum HelperError: Error, CustomStringConvertible {
    case invalidArguments
    case emptySecret
    case invalidSecretData
    case keychain(OSStatus)

    var description: String {
        switch self {
        case .invalidArguments:
            return "invalid arguments"
        case .emptySecret:
            return "secret is empty"
        case .invalidSecretData:
            return "secret is not valid UTF-8"
        case .keychain(let status):
            let message = SecCopyErrorMessageString(status, nil)
                as String? ?? "unknown Security error"
            return "Security error \(status): \(message)"
        }
    }
}

func query(
    service: String,
    account: String,
    dataProtection: Bool = false
) -> [String: Any] {
    var result: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: account,
    ]
    if dataProtection {
        result[kSecUseDataProtectionKeychain as String] = true
    }
    return result
}

func setSecret(service: String, account: String) throws {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard var secret = String(data: input, encoding: .utf8) else {
        throw HelperError.invalidSecretData
    }
    secret = secret.trimmingCharacters(in: .newlines)
    guard !secret.isEmpty else {
        throw HelperError.emptySecret
    }
    guard let secretData = secret.data(using: .utf8) else {
        throw HelperError.invalidSecretData
    }

    let itemQuery = query(
        service: service,
        account: account,
        dataProtection: true
    )
    let update: [String: Any] = [
        kSecValueData as String: secretData,
        kSecAttrAccessible as String:
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
    ]
    var status = SecItemUpdate(itemQuery as CFDictionary, update as CFDictionary)
    if status == errSecItemNotFound {
        var addition = itemQuery
        addition[kSecValueData as String] = secretData
        addition[kSecAttrAccessible as String] =
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        status = SecItemAdd(addition as CFDictionary, nil)
    }
    secret = ""
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
}

func dataProtectionAttributes(
    service: String,
    account: String
) throws -> [String: Any]? {
    var attributesQuery = query(
        service: service,
        account: account,
        dataProtection: true
    )
    attributesQuery[kSecReturnAttributes as String] = true
    attributesQuery[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let readStatus = SecItemCopyMatching(
        attributesQuery as CFDictionary,
        &result
    )
    if readStatus == errSecItemNotFound {
        return nil
    }
    guard readStatus == errSecSuccess else {
        throw HelperError.keychain(readStatus)
    }
    return result as? [String: Any]
}

func isAfterFirstUnlock(service: String, account: String) throws -> Bool {
    guard
        let attributes = try dataProtectionAttributes(
            service: service,
            account: account
        ),
        let accessibility = attributes[
            kSecAttrAccessible as String
        ] as? String
    else {
        return false
    }
    return accessibility
        == (kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String)
}

func legacySecretData(service: String, account: String) throws -> Data {
    var legacyQuery = query(service: service, account: account)
    legacyQuery[kSecReturnData as String] = true
    legacyQuery[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(
        legacyQuery as CFDictionary,
        &result
    )
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
    guard let data = result as? Data, !data.isEmpty else {
        throw HelperError.emptySecret
    }
    return data
}

func legacyItemExists(service: String, account: String) throws -> Bool {
    var legacyQuery = query(service: service, account: account)
    legacyQuery[kSecReturnData as String] = false
    legacyQuery[kSecMatchLimit as String] = kSecMatchLimitOne
    let status = SecItemCopyMatching(legacyQuery as CFDictionary, nil)
    if status == errSecItemNotFound {
        return false
    }
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
    return true
}

func migrateLegacyItem(service: String, account: String) throws {
    let secretData = try legacySecretData(
        service: service,
        account: account
    )
    var itemQuery = query(
        service: service,
        account: account,
        dataProtection: true
    )
    itemQuery[kSecValueData as String] = secretData
    itemQuery[kSecAttrAccessible as String] =
        kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    let status = SecItemAdd(itemQuery as CFDictionary, nil)
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
}

func ensureAfterFirstUnlock(service: String, account: String) throws {
    let attributes = try dataProtectionAttributes(
        service: service,
        account: account
    )
    if
        let accessibility = attributes?[
            kSecAttrAccessible as String
        ] as? String,
        accessibility
            == (kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String)
    {
        return
    }
    if attributes == nil {
        try migrateLegacyItem(service: service, account: account)
        return
    }

    let itemQuery = query(
        service: service,
        account: account,
        dataProtection: true
    )
    let update: [String: Any] = [
        kSecAttrAccessible as String:
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
    ]
    let status = SecItemUpdate(
        itemQuery as CFDictionary,
        update as CFDictionary
    )
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
}

func migrateIfPresent(service: String, account: String) throws {
    if try dataProtectionAttributes(
        service: service,
        account: account
    ) != nil {
        try ensureAfterFirstUnlock(service: service, account: account)
        return
    }
    if try legacyItemExists(service: service, account: account) {
        try migrateLegacyItem(service: service, account: account)
    }
}

func getSecret(service: String, account: String) throws {
    try ensureAfterFirstUnlock(service: service, account: account)
    var itemQuery = query(
        service: service,
        account: account,
        dataProtection: true
    )
    itemQuery[kSecReturnData as String] = true
    itemQuery[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(itemQuery as CFDictionary, &result)
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
    guard let data = result as? Data, !data.isEmpty else {
        throw HelperError.emptySecret
    }
    FileHandle.standardOutput.write(data)
}

func secretExists(service: String, account: String) throws {
    try ensureAfterFirstUnlock(service: service, account: account)
    var itemQuery = query(
        service: service,
        account: account,
        dataProtection: true
    )
    itemQuery[kSecReturnData as String] = false
    itemQuery[kSecMatchLimit as String] = kSecMatchLimitOne
    let status = SecItemCopyMatching(itemQuery as CFDictionary, nil)
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
}

do {
    guard CommandLine.arguments.count == 4 else {
        throw HelperError.invalidArguments
    }
    let command = CommandLine.arguments[1]
    let service = CommandLine.arguments[2]
    let account = CommandLine.arguments[3]
    switch command {
    case "set":
        try setSecret(service: service, account: account)
    case "get":
        try getSecret(service: service, account: account)
    case "exists":
        try secretExists(service: service, account: account)
    case "ensure-after-first-unlock":
        try ensureAfterFirstUnlock(service: service, account: account)
    case "migrate-if-present":
        try migrateIfPresent(service: service, account: account)
    case "is-after-first-unlock":
        if try !isAfterFirstUnlock(service: service, account: account) {
            exit(2)
        }
    default:
        throw HelperError.invalidArguments
    }
} catch let error as HelperError {
    FileHandle.standardError.write(
        Data("\(error)\n".utf8)
    )
    exit(1)
} catch {
    FileHandle.standardError.write(
        Data("unexpected error: \(error)\n".utf8)
    )
    exit(1)
}
