#import <Foundation/Foundation.h>
#import <ImageIO/ImageIO.h>
#import <Vision/Vision.h>
#import <math.h>

static void fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    exit(1);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 2) {
            fail(@"usage: native_ocr INPUT");
        }

        NSURL *inputURL = [NSURL fileURLWithPath:@(argv[1])];
        CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)inputURL, NULL);
        if (!source) fail(@"cannot open input image");
        CGImageRef image = CGImageSourceCreateImageAtIndex(source, 0, NULL);
        CFRelease(source);
        if (!image) fail(@"cannot decode input image");

        VNRecognizeTextRequest *request = [VNRecognizeTextRequest new];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = YES;
        request.recognitionLanguages = @[@"zh-Hans", @"en-US"];
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
        NSError *visionError = nil;
        BOOL succeeded = [handler performRequests:@[request] error:&visionError];
        CGImageRelease(image);
        if (!succeeded) {
            fail(visionError.localizedDescription ?: @"OCR failed");
        }

        NSArray<VNRecognizedTextObservation *> *observations = [request.results sortedArrayUsingComparator:
            ^NSComparisonResult(VNRecognizedTextObservation *left, VNRecognizedTextObservation *right) {
                CGFloat leftY = CGRectGetMidY(left.boundingBox);
                CGFloat rightY = CGRectGetMidY(right.boundingBox);
                if (fabs(leftY - rightY) > 0.015) {
                    return leftY > rightY ? NSOrderedAscending : NSOrderedDescending;
                }
                CGFloat leftX = CGRectGetMinX(left.boundingBox);
                CGFloat rightX = CGRectGetMinX(right.boundingBox);
                if (leftX < rightX) return NSOrderedAscending;
                if (leftX > rightX) return NSOrderedDescending;
                return NSOrderedSame;
            }
        ];
        for (VNRecognizedTextObservation *observation in observations) {
            VNRecognizedText *candidate = [observation topCandidates:1].firstObject;
            if (candidate.string.length > 0) {
                printf("%s\n", candidate.string.UTF8String);
            }
        }
    }
    return 0;
}
